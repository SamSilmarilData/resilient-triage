"""Tests for LangGraph cyclic state machine, self-repair loops, and resilience routing."""

import json
import time
import uuid

import httpx
import pytest
from fakes import make_triage_deps
from langgraph.runtime import Runtime

from resilient_triage.graph.builder import build_triage_graph
from resilient_triage.graph.llm import MockTriageChatModel
from resilient_triage.graph.nodes.analyze_query import analyze_query_node
from resilient_triage.graph.nodes.collect_telemetry import collect_telemetry_node
from resilient_triage.graph.nodes.validate_report import clean_json_string
from resilient_triage.resilience.circuit_breaker import force_open_for_tests
from resilient_triage.schemas.incident import (
    DegradationStatus,
    IncidentTriageReport,
    SeverityLevel,
    TelemetrySignal,
)


def _make_valid_report_json(severity: str = "HIGH", confidence: float = 0.90) -> str:
    return json.dumps(
        {
            "summary": "GitHub Actions queue latency spike",
            "severity": severity,
            "root_cause_analysis": "Upstream webhook processing congestion",
            "affected_services": [
                {
                    "service_name": "Actions Runner",
                    "impact_level": "Major Outage",
                    "details": "Job pickup delayed",
                }
            ],
            "telemetry_signals": [
                {
                    "source": "statuspage:global",
                    "status": "major",
                    "latency_ms": 280.0,
                    "error_rate": 0.5,
                    "raw_snippet": "GitHub: Major Service Outage",
                }
            ],
            "recommended_actions": [
                {
                    "priority": "P0",
                    "action": (
                        "Check runner controller logs: kubectl logs -n actions deploy/controller"
                    ),
                    "target_system": "Actions Controller",
                    "action_type": "diagnostic",
                    "requires_approval": False,
                },
                {
                    "priority": "P1",
                    "action": "Drain stale runner nodes and spawn fresh instance pool",
                    "target_system": "Runner Pool",
                    "action_type": "remediative",
                    "requires_approval": True,
                },
            ],
            "degradation_status": "HEALTHY",
            "circuit_breakers_tripped": [],
            "confidence_score": confidence,
        }
    )


def test_clean_json_string_utilities():
    """Verify markdown fences and surrounding text are cleanly stripped."""
    raw_markdown = '```json\n{"summary": "Test incident"}\n```'
    assert clean_json_string(raw_markdown) == '{"summary": "Test incident"}'

    conversational = (
        'Here is the incident triage report:\n\n{"summary": "Outage"}\n\nHope this helps!'
    )
    assert clean_json_string(conversational) == '{"summary": "Outage"}'


def test_dynamic_tool_targeting():
    """Verify analyze_query selectively targets relevant telemetry sources."""
    # External query -> statuspage
    state1 = analyze_query_node({"query": "GitHub Actions workflows are failing with 503"})
    assert state1["target_tools"] == ["statuspage"]

    # Internal query -> chaos
    state2 = analyze_query_node({"query": "Internal latency probe on auth-service"})
    assert state2["target_tools"] == ["chaos"]

    # Broad query -> both
    state3 = analyze_query_node({"query": "Service outage across entire infrastructure"})
    assert state3["target_tools"] == ["statuspage", "chaos"]


@pytest.mark.asyncio
async def test_graph_happy_path():
    """Verify end-to-end execution of LangGraph on happy path."""
    app = build_triage_graph(enable_checkpointing=True)
    thread_id = str(uuid.uuid4())

    inputs = {"query": "GitHub Actions jobs are queued indefinitely"}
    config = {"configurable": {"thread_id": thread_id}}

    final_state = await app.ainvoke(inputs, config=config, context=make_triage_deps())

    assert "final_report" in final_state
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert report.severity == SeverityLevel.HIGH
    assert report.degradation_status == DegradationStatus.HEALTHY
    assert len(report.recommended_actions) >= 1

    # Verify action safety classification
    diag = next((a for a in report.recommended_actions if a.action_type == "diagnostic"), None)
    assert diag is not None
    assert diag.requires_approval is False

    rem = next((a for a in report.recommended_actions if a.action_type == "remediative"), None)
    assert rem is not None
    assert rem.requires_approval is True


@pytest.mark.asyncio
async def test_graph_degraded_and_compensatory_routing():
    """Verify an OPEN breaker routes through compensatory cache and degraded fallback."""
    # Deliberately trip the chaos breaker
    deps = make_triage_deps()
    chaos_cb = deps.telemetry.breakers.get_or_create("chaos")
    force_open_for_tests(chaos_cb)
    assert chaos_cb.info().is_open is True

    app = build_triage_graph(enable_checkpointing=False)
    inputs = {
        "query": "Internal database chaos probe failing",
        "service_filter": "auth-service",
    }

    final_state = await app.ainvoke(inputs, context=deps)

    assert final_state["is_degraded"] is True
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert report.degradation_status != DegradationStatus.HEALTHY
    assert "chaos" in report.circuit_breakers_tripped
    assert len(final_state.get("historical_context", [])) >= 1


@pytest.mark.asyncio
async def test_graph_self_repair_on_bad_enum():
    """Verify self-repair loop catches bad enum, feeds traceback, and recovers on retry."""
    bad_enum_json = json.dumps(
        {
            "summary": "Outage detected",
            "severity": "CATASTROPHIC",  # Bad enum!
            "root_cause_analysis": "Unknown root cause",
            "confidence_score": 0.85,
        }
    )
    valid_json = _make_valid_report_json(severity="CRITICAL")

    mock_llm = MockTriageChatModel()
    mock_llm.responses = [bad_enum_json, valid_json]
    deps = make_triage_deps(llm=mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke(
        {"query": "Critical production outage on Actions"}, context=deps
    )

    assert final_state["repair_attempts"] == 1
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert report.severity == SeverityLevel.CRITICAL
    assert mock_llm.call_count == 2


@pytest.mark.asyncio
async def test_graph_self_repair_on_malformed_json():
    """Verify self-repair loop catches unparseable JSON and recovers on retry."""
    malformed_raw = "Error: System is malfunctioning, cannot output JSON!"
    valid_json = _make_valid_report_json(severity="HIGH")

    mock_llm = MockTriageChatModel()
    mock_llm.responses = [malformed_raw, valid_json]
    deps = make_triage_deps(llm=mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "Actions queue failing"}, context=deps)

    assert final_state["repair_attempts"] == 1
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert mock_llm.call_count == 2


@pytest.mark.asyncio
async def test_graph_self_repair_cap_routes_to_fallback():
    """Verify that after 2 exhausted self-repair retries the graph routes to the fallback report."""
    always_bad_json = "```Not json at all```"

    mock_llm = MockTriageChatModel()
    mock_llm.responses = [always_bad_json, always_bad_json, always_bad_json]
    deps = make_triage_deps(llm=mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "Permanent parser failure scenario"}, context=deps)

    # Should have attempted 2 repairs
    assert final_state["repair_attempts"] >= 2
    # Should have constructed a safe fallback report without failing the request
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert report.severity == SeverityLevel.UNKNOWN
    assert report.degradation_status == DegradationStatus.SEVERELY_DEGRADED
    assert report.confidence_score == 0.20
    assert "Self-repair" in report.root_cause_analysis


@pytest.mark.asyncio
async def test_graph_confidence_refinement_cycle():
    """Verify that low confidence (<0.60) triggers single reflection cycle."""
    low_conf_json = _make_valid_report_json(severity="MEDIUM", confidence=0.45)
    refined_conf_json = _make_valid_report_json(severity="MEDIUM", confidence=0.85)

    mock_llm = MockTriageChatModel()
    mock_llm.responses = [low_conf_json, refined_conf_json]
    deps = make_triage_deps(llm=mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "Uncertain intermittent latency spike"}, context=deps)

    assert final_state["refinement_attempts"] == 1
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert report.confidence_score == 0.85
    assert mock_llm.call_count == 2


@pytest.mark.asyncio
async def test_graph_thread_checkpointing():
    """Verify thread-scoped checkpointing maintains conversation context across calls."""
    app = build_triage_graph(enable_checkpointing=True)
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    deps = make_triage_deps()

    # Call 1
    res1 = await app.ainvoke(
        {"query": "Initial incident report on GitHub Actions"}, config=config, context=deps
    )
    messages_after_call1 = len(res1["messages"])

    # Call 2 with same thread_id
    res2 = await app.ainvoke(
        {"query": "What is the status of the runners now?"}, config=config, context=deps
    )
    messages_after_call2 = len(res2["messages"])

    # Message history should have accumulated across turns
    assert messages_after_call2 > messages_after_call1


def test_groq_chat_model_formatting_and_factory():
    """Verify GroqChatModel message formatting and active model reporting."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from resilient_triage.config import Settings
    from resilient_triage.graph.llm import GroqChatModel, GroqQuotaTracker
    from resilient_triage.runtime import build_runtime

    model = GroqChatModel(
        api_key="mock_key",
        model_name="qwen/qwen3.8-27b",
        quota_tracker=GroqQuotaTracker(),
        http_client=httpx.AsyncClient(),
    )
    assert model._llm_type == "groq-lpu-chat-model"

    msgs = [SystemMessage(content="You are SRE."), HumanMessage(content="Is Actions down?")]
    formatted = model._format_messages(msgs)
    assert formatted == [
        {"role": "system", "content": "You are SRE."},
        {"role": "user", "content": "Is Actions down?"},
    ]

    # Verify active model name reporting
    runtime = build_runtime(Settings(groq_api_key="mock_key", groq_model="qwen/qwen3.8-27b"))
    active_name = runtime.active_model
    assert isinstance(active_name, str)
    assert len(active_name) > 0
    assert active_name == "Groq LPU (qwen/qwen3.8-27b)"


def test_groq_chat_model_rejects_a_misspelled_argument():
    """A typo'd keyword fails loudly instead of being dropped for a default."""
    from pydantic import ValidationError

    from resilient_triage.graph.llm import GroqChatModel, GroqQuotaTracker

    with pytest.raises(ValidationError, match="fallback_modle"):
        GroqChatModel(
            api_key="mock_key",
            quota_tracker=GroqQuotaTracker(),
            http_client=httpx.AsyncClient(),
            fallback_modle="llama-3.1-8b-instant",
        )


@pytest.mark.asyncio
async def test_groq_chat_model_429_failover(mocker):
    """Verify GroqChatModel automatically fails over to fallback_model on HTTP 429."""
    from langchain_core.messages import HumanMessage

    from resilient_triage.graph.llm import GroqChatModel, GroqQuotaTracker

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    resp_429 = mocker.MagicMock(status_code=429, text="Rate limit exceeded")
    resp_429.raise_for_status.side_effect = httpx.HTTPStatusError(
        "429", request=mocker.MagicMock(), response=resp_429
    )
    resp_200 = mocker.MagicMock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": '{"summary": "Fallback OK"}'}}]},
    )
    mock_client.post.side_effect = [resp_429, resp_200]

    model = GroqChatModel(
        api_key="test_key",
        model_name="qwen/qwen3.8-27b",
        fallback_model="llama-3.1-8b-instant",
        quota_tracker=GroqQuotaTracker(),
        http_client=mock_client,
    )
    res = await model.ainvoke([HumanMessage(content="test")])
    assert res.content == '{"summary": "Fallback OK"}'
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_collect_telemetry_dynamic_probe_routing():
    """Verify collect_telemetry_node passes incident context to get domain-specific sources."""
    # Postgres query -> postgres_health_probe
    state_pg = {
        "query": "Database connection pool exhausted on orders-db",
        "service_filter": "postgres",
        "target_tools": ["chaos"],
    }
    res_pg = await collect_telemetry_node(state_pg, Runtime(context=make_triage_deps()))
    signals_pg = res_pg.get("telemetry_signals", [])
    assert len(signals_pg) == 1
    assert signals_pg[0]["source"] == "postgres_health_probe"

    # Gateway query -> api_gateway_probe
    state_gw = {
        "query": "ALB ingress router dropping HTTP connections",
        "service_filter": "api-gateway",
        "target_tools": ["chaos"],
    }
    res_gw = await collect_telemetry_node(state_gw, Runtime(context=make_triage_deps()))
    signals_gw = res_gw.get("telemetry_signals", [])
    assert len(signals_gw) == 1
    assert signals_gw[0]["source"] == "api_gateway_probe"


_UNAVAILABLE_SIGNAL = TelemetrySignal(
    source="statuspage:global", status="UNAVAILABLE", raw_snippet="Statuspage unavailable"
)


@pytest.mark.asyncio
async def test_collect_telemetry_records_source_whose_task_raised(mocker):
    """A targeted source whose fetch task raised is recorded as unavailable."""
    deps = make_triage_deps()
    mocker.patch.object(
        deps.telemetry.statuspage,
        "fetch_signals",
        new=mocker.AsyncMock(side_effect=RuntimeError("boom")),
    )

    result = await collect_telemetry_node(
        {"query": "q", "target_tools": ["statuspage"]}, Runtime(context=deps)
    )

    assert result["unavailable_sources"] == ["statuspage"]
    assert result["tripped_breakers"] == []


@pytest.mark.asyncio
async def test_collect_telemetry_tripped_breaker_is_not_unavailable(mocker):
    """A source whose breaker is tripped counts as a tripped breaker, not an unavailable source."""
    deps = make_triage_deps()
    force_open_for_tests(deps.telemetry.breakers.get_or_create("statuspage"))
    mocker.patch.object(
        deps.telemetry.statuspage,
        "fetch_signals",
        new=mocker.AsyncMock(return_value=[_UNAVAILABLE_SIGNAL]),
    )

    result = await collect_telemetry_node(
        {"query": "q", "target_tools": ["statuspage"]}, Runtime(context=deps)
    )

    assert result["tripped_breakers"] == ["statuspage"]
    assert result["unavailable_sources"] == []


@pytest.mark.asyncio
async def test_collect_telemetry_never_records_untargeted_source(mocker):
    """A source the query does not target is never recorded as unavailable."""
    deps = make_triage_deps()
    fetch = mocker.patch.object(
        deps.telemetry.statuspage,
        "fetch_signals",
        new=mocker.AsyncMock(side_effect=RuntimeError("x")),
    )

    result = await collect_telemetry_node(
        {"query": "Database connection pool exhausted", "target_tools": ["chaos"]},
        Runtime(context=deps),
    )

    fetch.assert_not_called()
    assert result["unavailable_sources"] == []


@pytest.mark.asyncio
async def test_graph_enforces_remediation_approval_safety():
    """Verify the graph's final report forces requires_approval=True on non-diagnostic actions."""
    candidate_with_unapproved_remediation = json.dumps(
        {
            "summary": "Kubernetes worker node pool CPU saturation",
            "severity": "CRITICAL",
            "root_cause_analysis": "OOM killer terminating runner processes",
            "affected_services": [
                {
                    "service_name": "worker-pool",
                    "impact_level": "Total Outage",
                    "details": "Pods crashing",
                }
            ],
            "telemetry_signals": [],
            "recommended_actions": [
                {
                    "priority": "P0",
                    "action": "kubectl drain node-pool-primary --delete-emptydir-data --force",
                    "target_system": "Kubelet",
                    "action_type": "Remediation",
                    "requires_approval": False,  # Safety violation from LLM
                },
                {
                    "priority": "P1",
                    "action": "kubectl top nodes",
                    "target_system": "Monitoring",
                    "action_type": "diagnostic",
                    "requires_approval": False,
                },
            ],
            "degradation_status": "HEALTHY",
            "circuit_breakers_tripped": [],
            "confidence_score": 0.95,
        }
    )

    mock_llm = MockTriageChatModel()
    mock_llm.responses = [candidate_with_unapproved_remediation]
    deps = make_triage_deps(llm=mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke(
        {"query": "Kubernetes worker node pool CPU saturation"}, context=deps
    )
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    # Non-diagnostic action must be programmatically forced to requires_approval=True
    assert report.recommended_actions[0].action_type == "Remediation"
    assert report.recommended_actions[0].requires_approval is True
    # Diagnostic action remains requires_approval=False
    assert report.recommended_actions[1].action_type == "diagnostic"
    assert report.recommended_actions[1].requires_approval is False


def test_bounded_memory_saver_eviction():
    """Verify BoundedMemorySaver evicts oldest thread checkpoints when capacity is reached."""
    from resilient_triage.graph.builder import BoundedMemorySaver

    saver = BoundedMemorySaver(max_threads=2)
    # Add checkpoints for 4 distinct thread IDs
    for i in range(4):
        cfg = {"configurable": {"thread_id": f"thread_{i}", "checkpoint_ns": ""}}
        saver.put(cfg, {"id": f"chk_{i}", "v": 1, "ts": i, "channel_values": {}}, {}, {})

    # Capacity must not exceed 2
    assert len(saver.storage) == 2
    # Only thread_2 and thread_3 should remain
    assert set(saver.storage.keys()) == {"thread_2", "thread_3"}


def test_reconcile_overrides_hallucinated_healthy_when_circuit_tripped():
    """Verify reconcile elevates degradation_status from HEALTHY when breakers are tripped."""
    from resilient_triage.graph.reconciliation import DegradationEvidence, reconcile
    from resilient_triage.schemas.incident import DegradationStatus

    report = IncidentTriageReport.model_validate(
        {
            "summary": "Partial database failover",
            "severity": "MEDIUM",
            "root_cause_analysis": "Primary node replication lag",
            "affected_services": [],
            "telemetry_signals": [],
            "recommended_actions": [],
            "degradation_status": "HEALTHY",  # Hallucinated HEALTHY
            "circuit_breakers_tripped": [],
            "confidence_score": 0.85,
        }
    )

    # 1 breaker tripped -> PARTIALLY_DEGRADED
    single = DegradationEvidence(
        tripped_breakers=frozenset({"chaos"}),
        unavailable_sources=frozenset(),
        synthesis_fell_back=False,
    )
    rep_single = reconcile(report, single)
    assert rep_single.degradation_status == DegradationStatus.PARTIALLY_DEGRADED
    assert rep_single.circuit_breakers_tripped == ["chaos"]

    # 2 breakers tripped -> SEVERELY_DEGRADED
    double = DegradationEvidence(
        tripped_breakers=frozenset({"chaos", "statuspage"}),
        unavailable_sources=frozenset(),
        synthesis_fell_back=False,
    )
    rep_double = reconcile(report, double)
    assert rep_double.degradation_status == DegradationStatus.SEVERELY_DEGRADED
    assert set(rep_double.circuit_breakers_tripped) == {"chaos", "statuspage"}


@pytest.mark.asyncio
async def test_groq_proactive_cooldown_routing(mocker):
    """Verify GroqChatModel routes to fallback_model during an active rate-limit cooldown."""
    from langchain_core.messages import HumanMessage

    from resilient_triage.graph.llm import GroqChatModel, GroqQuotaTracker

    # Set active cooldown window
    quota_tracker = GroqQuotaTracker()
    quota_tracker.rate_limit_cooldown_until = time.monotonic() + 10.0

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_resp = mocker.MagicMock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": '{"summary": "Fallback Cooldown OK"}'}}]},
    )
    mock_client.post.return_value = mock_resp

    model = GroqChatModel(
        api_key="test_key",
        model_name="qwen/qwen3.8-27b",
        fallback_model="llama-3.1-8b-instant",
        quota_tracker=quota_tracker,
        http_client=mock_client,
    )

    res = await model.ainvoke([HumanMessage(content="test query")])
    assert res.content == '{"summary": "Fallback Cooldown OK"}'

    # Must only make 1 call directly to fallback model without touching primary
    assert mock_client.post.call_count == 1
    call_payload = mock_client.post.call_args[1]["json"]
    assert call_payload["model"] == "llama-3.1-8b-instant"


@pytest.mark.asyncio
async def test_synthesis_fallback_status_is_never_downgraded():
    """Synthesis fallback with one tripped breaker gives SEVERELY_DEGRADED, not PARTIALLY."""
    mock_llm = MockTriageChatModel()
    mock_llm.responses = ["not json"] * 5
    deps = make_triage_deps(llm=mock_llm)
    force_open_for_tests(deps.telemetry.breakers.get_or_create("chaos"))

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke(
        {"query": "Internal chaos probe failing on auth-service"}, context=deps
    )

    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert report.confidence_score == 0.20
    assert report.circuit_breakers_tripped == ["chaos"]
    assert report.degradation_status == DegradationStatus.SEVERELY_DEGRADED


@pytest.mark.asyncio
async def test_only_targeted_breakers_degrade_the_report(mocker):
    """An open breaker the query never targets leaves the report HEALTHY."""
    deps = make_triage_deps()
    force_open_for_tests(deps.telemetry.breakers.get_or_create("chaos"))
    mocker.patch.object(
        deps.telemetry.statuspage, "fetch_signals", new=mocker.AsyncMock(return_value=[])
    )

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke(
        {"query": "GitHub Actions webhook delivery failing"}, context=deps
    )

    assert final_state["target_tools"] == ["statuspage"]
    report = final_state["final_report"]
    assert report.degradation_status == DegradationStatus.HEALTHY
    assert report.circuit_breakers_tripped == []


@pytest.mark.asyncio
async def test_llm_invented_breaker_names_are_dropped(mocker):
    """An LLM report claiming a tripped 'redis' breaker with no evidence gets an empty list."""
    claimed = json.loads(_make_valid_report_json())
    claimed["circuit_breakers_tripped"] = ["redis"]

    mock_llm = MockTriageChatModel()
    mock_llm.responses = [json.dumps(claimed)]
    deps = make_triage_deps(llm=mock_llm)
    mocker.patch.object(
        deps.telemetry.statuspage, "fetch_signals", new=mocker.AsyncMock(return_value=[])
    )

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke(
        {"query": "GitHub Actions webhook delivery failing"}, context=deps
    )

    report = final_state["final_report"]
    assert report.circuit_breakers_tripped == []
    assert report.degradation_status == DegradationStatus.HEALTHY


@pytest.mark.asyncio
async def test_unavailable_source_degrades_the_report(mocker):
    """A targeted source returning UNAVAILABLE with a closed breaker gives PARTIALLY_DEGRADED."""
    deps = make_triage_deps()
    mocker.patch.object(
        deps.telemetry.statuspage,
        "fetch_signals",
        new=mocker.AsyncMock(
            return_value=[
                TelemetrySignal(
                    source="statuspage:global",
                    status="UNAVAILABLE",
                    raw_snippet="Statuspage unavailable: connection reset",
                )
            ]
        ),
    )

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke(
        {"query": "GitHub Actions webhook delivery failing"}, context=deps
    )

    assert final_state["target_tools"] == ["statuspage"]
    assert not deps.telemetry.breakers.get_or_create("statuspage").info().is_open
    report = final_state["final_report"]
    assert report.circuit_breakers_tripped == []
    assert report.degradation_status == DegradationStatus.PARTIALLY_DEGRADED


def _groq_response(status_code: int, content: str = "{}") -> httpx.Response:
    """Build a Groq chat-completions response with the given status code."""
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    if status_code == 429:
        return httpx.Response(429, request=request, json={"error": {"message": "Rate limit"}})
    return httpx.Response(
        status_code, request=request, json={"choices": [{"message": {"content": content}}]}
    )


async def test_groq_models_with_separate_quota_trackers_do_not_share_rate_limit_counters(mocker):
    """A 429 seen by one Groq model leaves another model's quota tracker untouched."""
    from langchain_core.messages import HumanMessage

    from resilient_triage.graph.llm import GroqChatModel, GroqQuotaTracker

    client_a = mocker.AsyncMock(spec=httpx.AsyncClient)
    client_a.post.side_effect = [_groq_response(429), _groq_response(200, "fallback")]
    client_b = mocker.AsyncMock(spec=httpx.AsyncClient)
    client_b.post.return_value = _groq_response(200, "primary")
    tracker_a = GroqQuotaTracker()
    tracker_b = GroqQuotaTracker()
    model_a = GroqChatModel(api_key="k", quota_tracker=tracker_a, http_client=client_a)
    model_b = GroqChatModel(api_key="k", quota_tracker=tracker_b, http_client=client_b)

    assert (await model_a.ainvoke([HumanMessage(content="q")])).content == "fallback"
    assert (await model_b.ainvoke([HumanMessage(content="q")])).content == "primary"

    assert tracker_a.get_snapshot().rate_limit_hits == 1
    assert tracker_b.get_snapshot().rate_limit_hits == 0
    assert tracker_b.get_snapshot().total_calls == 1
    assert client_b.post.call_args[1]["json"]["model"] == "qwen/qwen3.8-27b"


def test_groq_model_requires_its_quota_tracker_and_http_client():
    """A Groq model can't silently fall back to shared state: both collaborators are required."""
    from pydantic import ValidationError

    from resilient_triage.graph.llm import GroqChatModel, GroqQuotaTracker

    with pytest.raises(ValidationError, match="http_client"):
        GroqChatModel(api_key="k", quota_tracker=GroqQuotaTracker())
    with pytest.raises(ValidationError, match="quota_tracker"):
        GroqChatModel(api_key="k", http_client=httpx.AsyncClient())
