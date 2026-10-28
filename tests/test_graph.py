"""Tests for LangGraph cyclic state machine, self-repair loops, and resilience routing."""

import json
import time
import uuid

import httpx
import pytest

from resilient_triage.graph.builder import build_triage_graph
from resilient_triage.graph.llm import MockTriageChatModel, set_triage_llm
from resilient_triage.graph.nodes.analyze_query import analyze_query_node
from resilient_triage.graph.nodes.collect_telemetry import collect_telemetry_node
from resilient_triage.graph.nodes.validate_report import clean_json_string
from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
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
                    "action": "Check runner controller logs: kubectl logs -n actions deploy/controller",
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

    final_state = await app.ainvoke(inputs, config=config)

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
    """Verify graph routes through compensatory cache and degraded fallback when circuit breaker is OPEN."""
    # Deliberately trip the chaos breaker
    chaos_cb = circuit_breaker_registry.get_or_create("chaos")
    chaos_cb.breaker.open()
    assert chaos_cb.is_open is True

    app = build_triage_graph(enable_checkpointing=False)
    inputs = {
        "query": "Internal database chaos probe failing",
        "service_filter": "auth-service",
    }

    final_state = await app.ainvoke(inputs)

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
    set_triage_llm(mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "Critical production outage on Actions"})

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
    set_triage_llm(mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "Actions queue failing"})

    assert final_state["repair_attempts"] == 1
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert mock_llm.call_count == 2


@pytest.mark.asyncio
async def test_graph_self_repair_cap_routes_to_fallback():
    """Verify that when 2 self-repair retries are exhausted, graph routes safely to fallback report."""
    always_bad_json = "```Not json at all```"

    mock_llm = MockTriageChatModel()
    mock_llm.responses = [always_bad_json, always_bad_json, always_bad_json]
    set_triage_llm(mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "Permanent parser failure scenario"})

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
    set_triage_llm(mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "Uncertain intermittent latency spike"})

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

    # Call 1
    res1 = await app.ainvoke({"query": "Initial incident report on GitHub Actions"}, config=config)
    messages_after_call1 = len(res1["messages"])

    # Call 2 with same thread_id
    res2 = await app.ainvoke({"query": "What is the status of the runners now?"}, config=config)
    messages_after_call2 = len(res2["messages"])

    # Message history should have accumulated across turns
    assert messages_after_call2 > messages_after_call1


def test_groq_chat_model_formatting_and_factory():
    """Verify GroqChatModel message formatting and active model reporting."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from resilient_triage.graph.llm import GroqChatModel, get_active_model_name

    model = GroqChatModel(api_key="mock_key", model_name="qwen/qwen3.8-27b")
    assert model._llm_type == "groq-lpu-chat-model"

    msgs = [SystemMessage(content="You are SRE."), HumanMessage(content="Is Actions down?")]
    formatted = model._format_messages(msgs)
    assert formatted == [
        {"role": "system", "content": "You are SRE."},
        {"role": "user", "content": "Is Actions down?"},
    ]

    # Verify active model name reporting
    active_name = get_active_model_name()
    assert isinstance(active_name, str)
    assert len(active_name) > 0


@pytest.mark.asyncio
async def test_groq_chat_model_429_failover(mocker):
    """Verify GroqChatModel automatically fails over to fallback_model on HTTP 429."""
    from langchain_core.messages import HumanMessage

    from resilient_triage.graph.llm import GroqChatModel

    mock_client = mocker.AsyncMock()
    resp_429 = mocker.MagicMock(status_code=429, text="Rate limit exceeded")
    resp_429.raise_for_status.side_effect = httpx.HTTPStatusError(
        "429", request=mocker.MagicMock(), response=resp_429
    )
    resp_200 = mocker.MagicMock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": '{"summary": "Fallback OK"}'}}]},
    )
    mock_client.post.side_effect = [resp_429, resp_200]
    mocker.patch("resilient_triage.graph.llm.get_groq_async_client", return_value=mock_client)

    model = GroqChatModel(
        api_key="test_key",
        model_name="qwen/qwen3.8-27b",
        fallback_model="llama-3.1-8b-instant",
    )
    res = await model.ainvoke([HumanMessage(content="test")])
    assert res.content == '{"summary": "Fallback OK"}'
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_collect_telemetry_dynamic_probe_routing():
    """Verify collect_telemetry_node passes incident context to produce domain-specific probe sources."""
    # Postgres query -> postgres_health_probe
    state_pg = {
        "query": "Database connection pool exhausted on orders-db",
        "service_filter": "postgres",
        "target_tools": ["chaos"],
    }
    res_pg = await collect_telemetry_node(state_pg)
    signals_pg = res_pg.get("telemetry_signals", [])
    assert len(signals_pg) == 1
    assert signals_pg[0]["source"] == "postgres_health_probe"

    # Gateway query -> api_gateway_probe
    state_gw = {
        "query": "ALB ingress router dropping HTTP connections",
        "service_filter": "api-gateway",
        "target_tools": ["chaos"],
    }
    res_gw = await collect_telemetry_node(state_gw)
    signals_gw = res_gw.get("telemetry_signals", [])
    assert len(signals_gw) == 1
    assert signals_gw[0]["source"] == "api_gateway_probe"


_STATUSPAGE_FETCH = "resilient_triage.graph.nodes.collect_telemetry.StatuspageClient.fetch_signals"
_UNAVAILABLE_SIGNAL = TelemetrySignal(
    source="statuspage:global", status="UNAVAILABLE", raw_snippet="Statuspage unavailable"
)


@pytest.mark.asyncio
async def test_collect_telemetry_records_source_whose_task_raised(mocker):
    """A targeted source whose fetch task raised is recorded as unavailable."""
    mocker.patch(_STATUSPAGE_FETCH, new=mocker.AsyncMock(side_effect=RuntimeError("boom")))

    result = await collect_telemetry_node({"query": "q", "target_tools": ["statuspage"]})

    assert result["unavailable_sources"] == ["statuspage"]
    assert result["tripped_breakers"] == []


@pytest.mark.asyncio
async def test_collect_telemetry_tripped_breaker_is_not_unavailable(mocker):
    """A source whose breaker is tripped counts as a tripped breaker, not an unavailable source."""
    circuit_breaker_registry.get_or_create("statuspage").breaker.open()
    mocker.patch(_STATUSPAGE_FETCH, new=mocker.AsyncMock(return_value=[_UNAVAILABLE_SIGNAL]))

    result = await collect_telemetry_node({"query": "q", "target_tools": ["statuspage"]})

    assert result["tripped_breakers"] == ["statuspage"]
    assert result["unavailable_sources"] == []


@pytest.mark.asyncio
async def test_collect_telemetry_never_records_untargeted_source(mocker):
    """A source the query does not target is never recorded as unavailable."""
    fetch = mocker.patch(_STATUSPAGE_FETCH, new=mocker.AsyncMock(side_effect=RuntimeError("x")))

    result = await collect_telemetry_node(
        {"query": "Database connection pool exhausted", "target_tools": ["chaos"]}
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
    set_triage_llm(mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "Kubernetes worker node pool CPU saturation"})
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
    """Verify GroqChatModel proactively routes to fallback_model during active rate-limit cooldown."""
    from langchain_core.messages import HumanMessage

    from resilient_triage.graph.llm import GroqChatModel, groq_quota_tracker

    # Set active cooldown window
    groq_quota_tracker.rate_limit_cooldown_until = time.monotonic() + 10.0

    mock_client = mocker.AsyncMock()
    mock_resp = mocker.MagicMock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": '{"summary": "Fallback Cooldown OK"}'}}]},
    )
    mock_client.post.return_value = mock_resp
    mocker.patch("resilient_triage.graph.llm.get_groq_async_client", return_value=mock_client)

    model = GroqChatModel(
        api_key="test_key",
        model_name="qwen/qwen3.8-27b",
        fallback_model="llama-3.1-8b-instant",
    )

    res = await model.ainvoke([HumanMessage(content="test query")])
    assert res.content == '{"summary": "Fallback Cooldown OK"}'

    # Must only make 1 call directly to fallback model without touching primary
    assert mock_client.post.call_count == 1
    call_payload = mock_client.post.call_args[1]["json"]
    assert call_payload["model"] == "llama-3.1-8b-instant"

    groq_quota_tracker.reset_counters()


@pytest.mark.asyncio
async def test_synthesis_fallback_status_is_never_downgraded():
    """Synthesis fallback with one tripped breaker gives SEVERELY_DEGRADED, not PARTIALLY."""
    circuit_breaker_registry.get_or_create("chaos").breaker.open()

    mock_llm = MockTriageChatModel()
    mock_llm.responses = ["not json"] * 5
    set_triage_llm(mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "Internal chaos probe failing on auth-service"})

    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert report.confidence_score == 0.20
    assert report.circuit_breakers_tripped == ["chaos"]
    assert report.degradation_status == DegradationStatus.SEVERELY_DEGRADED


@pytest.mark.asyncio
async def test_only_targeted_breakers_degrade_the_report(mocker):
    """An open breaker the query never targets leaves the report HEALTHY."""
    circuit_breaker_registry.get_or_create("chaos").breaker.open()
    mocker.patch(
        "resilient_triage.graph.nodes.collect_telemetry.StatuspageClient.fetch_signals",
        new=mocker.AsyncMock(return_value=[]),
    )

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "GitHub Actions webhook delivery failing"})

    assert final_state["target_tools"] == ["statuspage"]
    report = final_state["final_report"]
    assert report.degradation_status == DegradationStatus.HEALTHY
    assert report.circuit_breakers_tripped == []


@pytest.mark.asyncio
async def test_llm_invented_breaker_names_are_dropped(mocker):
    """An LLM report claiming a tripped 'redis' breaker with no evidence gets an empty list."""
    mocker.patch(
        "resilient_triage.graph.nodes.collect_telemetry.StatuspageClient.fetch_signals",
        new=mocker.AsyncMock(return_value=[]),
    )
    claimed = json.loads(_make_valid_report_json())
    claimed["circuit_breakers_tripped"] = ["redis"]

    mock_llm = MockTriageChatModel()
    mock_llm.responses = [json.dumps(claimed)]
    set_triage_llm(mock_llm)

    app = build_triage_graph(enable_checkpointing=False)
    final_state = await app.ainvoke({"query": "GitHub Actions webhook delivery failing"})

    report = final_state["final_report"]
    assert report.circuit_breakers_tripped == []
    assert report.degradation_status == DegradationStatus.HEALTHY


@pytest.mark.asyncio
async def test_unavailable_source_degrades_the_report(mocker):
    """A targeted source returning UNAVAILABLE with a closed breaker gives PARTIALLY_DEGRADED."""
    mocker.patch(
        "resilient_triage.graph.nodes.collect_telemetry.StatuspageClient.fetch_signals",
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
    final_state = await app.ainvoke({"query": "GitHub Actions webhook delivery failing"})

    assert final_state["target_tools"] == ["statuspage"]
    assert not circuit_breaker_registry.get_or_create("statuspage").is_open
    report = final_state["final_report"]
    assert report.circuit_breakers_tripped == []
    assert report.degradation_status == DegradationStatus.PARTIALLY_DEGRADED
