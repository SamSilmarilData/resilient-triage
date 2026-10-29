"""Tests for LangGraph cyclic state machine, self-repair loops, and resilience routing."""

import json
from typing import Any

import httpx
import pytest
from fakes import make_triage_deps
from langgraph.runtime import Runtime

from resilient_triage.config import settings
from resilient_triage.graph.builder import build_triage_graph
from resilient_triage.graph.nodes.analyze_query import analyze_query_node
from resilient_triage.graph.nodes.collect_telemetry import collect_telemetry_node
from resilient_triage.graph.nodes.fallback_report import build_fallback_report, fallback_report_node
from resilient_triage.graph.nodes.validate_report import clean_json_string
from resilient_triage.llm import (
    MalformedResponse,
    ProviderFailure,
    RateLimited,
    Rejected,
    ScriptedAdapter,
    Unavailable,
)
from resilient_triage.resilience.circuit_breaker import force_open_for_tests
from resilient_triage.schemas.incident import (
    DegradationStatus,
    IncidentTriageReport,
    PriorityLevel,
    SeverityLevel,
    TelemetrySignal,
)


async def _run_recording_nodes(
    app: Any, inputs: dict[str, Any], deps: Any
) -> tuple[list[str], dict[str, Any]]:
    """Run the graph once; return the nodes it visited, in order, and its final state."""
    visited: list[str] = []
    final_state: dict[str, Any] = {}
    async for mode, chunk in app.astream(inputs, context=deps, stream_mode=["updates", "values"]):
        if mode == "updates":
            visited.extend(chunk)
        else:
            final_state = chunk
    return visited, final_state


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


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # "repo" inside "reporting" and "auth" inside "author" must not match
        ("Kubernetes ingress reporting 502 errors", ["statuspage", "chaos"]),
        ("Kubernetes ingress 502 errors", ["statuspage", "chaos"]),
        # "actions" inside "transactions" must not match
        ("Payment transactions failing at checkout", ["chaos"]),
        ("Pod author label missing after deploy", ["statuspage", "chaos"]),
        # strict whole word: plurals do not match their singular keyword
        ("Webhooks delayed for all repos", ["statuspage", "chaos"]),
        # underscore is part of a word
        ("chaos_probe reports failure", ["chaos"]),
        ("my_auth_helper timing out", ["statuspage", "chaos"]),
    ],
)
def test_tool_targeting_matches_whole_words(query, expected):
    """Keywords count only as whole words or phrases, never as substrings."""
    assert analyze_query_node({"query": query})["target_tools"] == expected


@pytest.mark.parametrize(
    ("service_filter", "expected"),
    [
        ("auth-service", ["chaos"]),
        ("author-service", ["statuspage", "chaos"]),
        ("github", ["statuspage"]),
    ],
)
def test_tool_targeting_service_filter_matches_whole_words(service_filter, expected):
    """A keyword in service_filter gets the same whole-word treatment as the query."""
    state = analyze_query_node({"query": "Errors spiking", "service_filter": service_filter})
    assert state["target_tools"] == expected


@pytest.mark.asyncio
async def test_graph_happy_path():
    """Verify end-to-end execution of LangGraph on happy path."""
    app = build_triage_graph()

    inputs = {"query": "GitHub Actions jobs are queued indefinitely"}

    final_state = await app.ainvoke(inputs, context=make_triage_deps())

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

    app = build_triage_graph()
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

    mock_llm = ScriptedAdapter([bad_enum_json, valid_json])
    deps = make_triage_deps(llm_adapter=mock_llm)

    app = build_triage_graph()
    final_state = await app.ainvoke(
        {"query": "Critical production outage on Actions"}, context=deps
    )

    assert final_state["repair_attempts"] == 1
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert report.severity == SeverityLevel.CRITICAL
    assert len(mock_llm.calls) == 2


@pytest.mark.asyncio
async def test_graph_self_repair_on_malformed_json():
    """Verify self-repair loop catches unparseable JSON and recovers on retry."""
    malformed_raw = "Error: System is malfunctioning, cannot output JSON!"
    valid_json = _make_valid_report_json(severity="HIGH")

    mock_llm = ScriptedAdapter([malformed_raw, valid_json])
    deps = make_triage_deps(llm_adapter=mock_llm)

    app = build_triage_graph()
    final_state = await app.ainvoke({"query": "Actions queue failing"}, context=deps)

    assert final_state["repair_attempts"] == 1
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert len(mock_llm.calls) == 2


@pytest.mark.asyncio
async def test_graph_self_repair_cap_routes_to_fallback():
    """Verify that after 2 exhausted self-repair retries the graph routes to the fallback report."""
    always_bad_json = "```Not json at all```"

    mock_llm = ScriptedAdapter([always_bad_json, always_bad_json, always_bad_json])
    deps = make_triage_deps(llm_adapter=mock_llm)

    app = build_triage_graph()
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


@pytest.mark.parametrize(
    "failure",
    [
        RateLimited(retry_after_s=30.0, scope="minute", detail="Rate limit"),
        Unavailable(detail="HTTP 503: upstream overloaded"),
        Rejected(detail="HTTP 400: bad model"),
        MalformedResponse(detail="no choices"),
    ],
)
async def test_provider_failure_on_first_generation_skips_self_repair(failure: ProviderFailure):
    """A provider failure goes straight to fallback_report: re-prompting cannot fix an outage."""
    deps = make_triage_deps(llm_adapter=ScriptedAdapter([failure] * 3))
    app = build_triage_graph()

    visited, final_state = await _run_recording_nodes(app, {"query": "Actions queue failing"}, deps)

    assert visited[-3:] == ["generate_report", "fallback_report", "finalize_report"]
    assert "self_repair" not in visited
    assert "validate_report" not in visited
    # The failure survives into the run's final state, typed.
    assert final_state["provider_failure"] == failure
    report = final_state["final_report"]
    assert report.degradation_status == DegradationStatus.SEVERELY_DEGRADED
    assert report.confidence_score == 0.20


UNAVAILABLE_TEXT = "Upstream LLM provider temporarily unavailable. Safe heuristic fallback engaged."


@pytest.mark.parametrize(
    ("failure", "root_cause"),
    [
        (
            RateLimited(retry_after_s=None, scope="day", detail="quota"),
            "LLM provider rate limit reached (HTTP 429). Degraded heuristic fallback analysis "
            "engaged.",
        ),
        (Unavailable(detail="HTTP 503: upstream overloaded"), UNAVAILABLE_TEXT),
        (MalformedResponse(detail="no choices"), UNAVAILABLE_TEXT),
        (
            Rejected(detail="HTTP 400: bad model"),
            "LLM provider rejected the request. Safe heuristic fallback engaged.",
        ),
    ],
)
def test_fallback_report_wording_follows_the_failure_kind(
    failure: ProviderFailure, root_cause: str
):
    """The fallback root cause names the provider failure kind, whatever its detail text says."""
    update = fallback_report_node(
        {"query": "Actions queue failing", "provider_failure": failure, "validation_errors": []}
    )

    report = update["final_report"]
    assert report.root_cause_analysis == root_cause
    assert report.severity == SeverityLevel.UNKNOWN
    assert report.confidence_score == 0.20
    assert update["synthesis_fell_back"] is True


def test_fallback_report_without_a_provider_failure_reports_exhausted_self_repair():
    """With no provider failure, the fallback means self-repair ran out, whatever the errors say."""
    update = fallback_report_node(
        {
            "query": "Actions queue failing",
            "provider_failure": None,
            "validation_errors": ["Field 'severity': HTTP 429 lookalike (input was: '503')"],
        }
    )

    root_cause = update["final_report"].root_cause_analysis
    assert root_cause.startswith("Self-repair limits reached. Diagnostic context: Field 'severity'")
    assert update["synthesis_fell_back"] is True


def test_build_fallback_report_needs_no_state():
    """Code outside the graph builds a synthesis fallback from a query, a reason and signals."""
    signal = TelemetrySignal(
        source="statuspage:global", status="DEGRADED", raw_snippet="Actions: degraded"
    )

    report = build_fallback_report(
        "Actions queue failing", reason="Graph raised RuntimeError", signals=[signal]
    )

    assert report.root_cause_analysis == "Graph raised RuntimeError"
    assert report.summary == "Automated incident triage degraded for: Actions queue failing"
    assert report.telemetry_signals == [signal]
    assert report.severity == SeverityLevel.UNKNOWN
    assert report.confidence_score == 0.20
    [action] = report.recommended_actions
    assert action.priority == PriorityLevel.P0
    assert action.action_type == "diagnostic"
    assert action.requires_approval is False


def test_build_fallback_report_defaults_to_no_signals():
    """Without signals the fallback carries none."""
    report = build_fallback_report("Actions queue failing", reason="no final report")

    assert report.telemetry_signals == []


def test_fallback_report_node_keeps_only_the_valid_telemetry_signals():
    """The node passes the state's valid signals to the report and drops malformed ones."""
    valid = {"source": "statuspage:global", "status": "OK", "raw_snippet": "All systems go"}

    update = fallback_report_node(
        {
            "query": "Actions queue failing",
            "provider_failure": None,
            "validation_errors": [],
            "telemetry_signals": [valid, {"status": "not-a-signal"}],
        }
    )

    assert update["final_report"].telemetry_signals == [TelemetrySignal.model_validate(valid)]
    assert update["is_degraded"] is True


@pytest.mark.asyncio
async def test_graph_confidence_refinement_cycle():
    """Verify that low confidence (<0.60) triggers single reflection cycle."""
    low_conf_json = _make_valid_report_json(severity="MEDIUM", confidence=0.45)
    refined_conf_json = _make_valid_report_json(severity="MEDIUM", confidence=0.85)

    mock_llm = ScriptedAdapter([low_conf_json, refined_conf_json])
    deps = make_triage_deps(llm_adapter=mock_llm)

    app = build_triage_graph()
    final_state = await app.ainvoke({"query": "Uncertain intermittent latency spike"}, context=deps)

    assert final_state["refinement_attempts"] == 1
    report = final_state["final_report"]
    assert isinstance(report, IncidentTriageReport)
    assert report.confidence_score == 0.85
    assert len(mock_llm.calls) == 2


async def test_refine_round_that_exhausts_self_repair_keeps_the_pre_refine_report():
    """A refine round whose output never validates finalizes the valid report from before it."""
    pre_refine_json = _make_valid_report_json(severity="MEDIUM", confidence=0.50)
    mock_llm = ScriptedAdapter([pre_refine_json, "not json", "still not json"])
    deps = make_triage_deps(llm_adapter=mock_llm)
    app = build_triage_graph()

    visited, final_state = await _run_recording_nodes(app, {"query": "Actions queue failing"}, deps)

    assert "refine_reflection" in visited
    assert "fallback_report" not in visited
    assert visited[-2:] == ["validate_report", "finalize_report"]
    assert len(mock_llm.calls) == 3  # the refine round shares the 2-repair budget
    assert final_state["synthesis_fell_back"] is False
    report = final_state["final_report"]
    assert report.confidence_score == 0.50
    assert report.severity == SeverityLevel.MEDIUM
    # No breaker tripped and no source unavailable: telemetry evidence says HEALTHY.
    assert report.degradation_status == DegradationStatus.HEALTHY


async def test_provider_failure_during_a_refine_round_keeps_the_pre_refine_report():
    """A provider outage mid-refine finalizes the valid report from before the refine round."""
    pre_refine_json = _make_valid_report_json(severity="MEDIUM", confidence=0.50)
    outage = Unavailable(detail="HTTP 503: upstream overloaded")
    mock_llm = ScriptedAdapter([pre_refine_json, outage, outage, outage])
    deps = make_triage_deps(llm_adapter=mock_llm)
    app = build_triage_graph()

    visited, final_state = await _run_recording_nodes(app, {"query": "Actions queue failing"}, deps)

    assert "refine_reflection" in visited
    assert "self_repair" not in visited
    assert "fallback_report" not in visited
    assert visited[-2:] == ["generate_report", "finalize_report"]
    assert final_state["synthesis_fell_back"] is False
    report = final_state["final_report"]
    assert report.confidence_score == 0.50
    assert report.severity == SeverityLevel.MEDIUM
    assert report.degradation_status != DegradationStatus.SEVERELY_DEGRADED


async def test_longest_path_stays_within_the_recursion_limit(monkeypatch):
    """Max self-repair, a tripped breaker and a failing refine round still fit 24 supersteps."""
    monkeypatch.setattr(settings, "max_self_repair_retries", 5)
    pre_refine_json = _make_valid_report_json(confidence=0.50)
    mock_llm = ScriptedAdapter([pre_refine_json] + ["not json"] * 5)
    deps = make_triage_deps(llm_adapter=mock_llm)
    force_open_for_tests(deps.telemetry.breakers.get_or_create("chaos"))
    app = build_triage_graph()

    visited = [
        node
        async for update in app.astream(
            {"query": "Internal chaos probe failing on auth-service"},
            context=deps,
            stream_mode="updates",
        )
        for node in update
    ]

    assert visited[-1] == "finalize_report"
    assert len(visited) <= 24
    assert len(mock_llm.calls) == 6


async def test_runs_of_the_same_query_keep_no_state_between_them():
    """Each run is self-contained: no checkpointer carries state from one run into the next."""
    app = build_triage_graph()
    deps = make_triage_deps()
    query = {"query": "Initial incident report on GitHub Actions"}

    first = await app.ainvoke(query, context=deps)
    second = await app.ainvoke(query, context=deps)

    assert len(first["messages"]) > 0
    # The second run's messages start fresh: none of the first run's messages carry over.
    assert len(second["messages"]) == len(first["messages"])
    first_ids = {message.id for message in first["messages"]}
    assert first_ids.isdisjoint(message.id for message in second["messages"])
    assert second["repair_attempts"] == first["repair_attempts"]
    assert second["refinement_attempts"] == first["refinement_attempts"]


def test_groq_adapter_formats_messages_and_the_runtime_labels_groq():
    """Verify GroqAdapter message formatting and active model reporting."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from resilient_triage.config import Settings
    from resilient_triage.llm import GroqAdapter, ModelAdapter
    from resilient_triage.llm.groq import format_messages
    from resilient_triage.runtime import build_runtime

    adapter: ModelAdapter = GroqAdapter("mock_key", timeout_s=5.0)
    assert isinstance(adapter, GroqAdapter)

    msgs = [SystemMessage(content="You are SRE."), HumanMessage(content="Is Actions down?")]
    formatted = format_messages(msgs)
    assert formatted == [
        {"role": "system", "content": "You are SRE."},
        {"role": "user", "content": "Is Actions down?"},
    ]

    # Verify active model name reporting
    runtime = build_runtime(Settings(groq_api_key="mock_key", groq_model="qwen/qwen3.8-27b"))
    active_name = runtime.llm.label
    assert isinstance(active_name, str)
    assert len(active_name) > 0
    assert active_name == "Groq LPU (qwen/qwen3.8-27b)"


def test_llm_gateway_rejects_a_misspelled_argument():
    """A typo'd keyword fails loudly instead of being dropped for a default."""
    from resilient_triage.llm import LLMGateway, ScriptedAdapter

    with pytest.raises(TypeError, match="fallback_modle"):
        LLMGateway(  # type: ignore[call-arg]
            ScriptedAdapter(),
            primary="qwen/qwen3.8-27b",
            label="test",
            fallback_modle="llama-3.1-8b-instant",
        )


@pytest.mark.asyncio
async def test_llm_gateway_fails_over_on_a_groq_429():
    """Verify the gateway fails over to the fallback model when Groq returns HTTP 429."""
    from langchain_core.messages import HumanMessage

    sent: list[dict] = []
    gateway = _groq_gateway(
        [_groq_response(429), _groq_response(200, '{"summary": "Fallback OK"}')], sent
    )
    res = await gateway.complete([HumanMessage(content="test")])
    assert res.text == '{"summary": "Fallback OK"}'
    assert len(sent) == 2


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

    mock_llm = ScriptedAdapter([candidate_with_unapproved_remediation])
    deps = make_triage_deps(llm_adapter=mock_llm)

    app = build_triage_graph()
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
async def test_groq_proactive_cooldown_routing():
    """Verify the gateway routes to the fallback model during an active rate-limit cooldown."""
    from langchain_core.messages import HumanMessage

    sent: list[dict] = []
    gateway = _groq_gateway(
        [
            _groq_response(429),
            _groq_response(200, '{"summary": "Failover OK"}'),
            _groq_response(200, '{"summary": "Fallback Cooldown OK"}'),
        ],
        sent,
    )
    # A 429 on the primary starts its cooldown window.
    await gateway.complete([HumanMessage(content="first query")])
    sent.clear()

    res = await gateway.complete([HumanMessage(content="test query")])
    assert res.text == '{"summary": "Fallback Cooldown OK"}'

    # Must only make 1 call directly to fallback model without touching primary
    assert len(sent) == 1
    call_payload = sent[0]
    assert call_payload["model"] == "llama-3.1-8b-instant"


@pytest.mark.asyncio
async def test_synthesis_fallback_status_is_never_downgraded():
    """Synthesis fallback with one tripped breaker gives SEVERELY_DEGRADED, not PARTIALLY."""
    mock_llm = ScriptedAdapter(["not json"] * 5)
    deps = make_triage_deps(llm_adapter=mock_llm)
    force_open_for_tests(deps.telemetry.breakers.get_or_create("chaos"))

    app = build_triage_graph()
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

    app = build_triage_graph()
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

    mock_llm = ScriptedAdapter([json.dumps(claimed)])
    deps = make_triage_deps(llm_adapter=mock_llm)
    mocker.patch.object(
        deps.telemetry.statuspage, "fetch_signals", new=mocker.AsyncMock(return_value=[])
    )

    app = build_triage_graph()
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

    app = build_triage_graph()
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
    if status_code == 429:
        return httpx.Response(429, json={"error": {"message": "Rate limit"}})
    return httpx.Response(status_code, json={"choices": [{"message": {"content": content}}]})


def _groq_gateway(responses: list[httpx.Response], sent: list[dict]):
    """An LLM gateway over a GroqAdapter whose HTTP calls get the given responses in order."""
    from resilient_triage.config import settings
    from resilient_triage.llm import GroqAdapter, build_llm_gateway

    replies = iter(responses)

    def groq_api(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return next(replies)

    http = httpx.AsyncClient(transport=httpx.MockTransport(groq_api))
    adapter = GroqAdapter("test_key", timeout_s=5.0, http_client=http)
    return build_llm_gateway(
        settings.model_copy(update={"groq_model": "qwen/qwen3.8-27b"}), adapter
    )


async def test_separate_groq_gateways_do_not_share_rate_limit_counters():
    """A 429 seen by one Groq gateway leaves another gateway's quota counters untouched."""
    from langchain_core.messages import HumanMessage

    sent_a: list[dict] = []
    sent_b: list[dict] = []
    gateway_a = _groq_gateway([_groq_response(429), _groq_response(200, "fallback")], sent_a)
    gateway_b = _groq_gateway([_groq_response(200, "primary")], sent_b)

    assert (await gateway_a.complete([HumanMessage(content="q")])).text == "fallback"
    assert (await gateway_b.complete([HumanMessage(content="q")])).text == "primary"

    assert gateway_a.snapshot().rate_limit_hits == 1
    assert gateway_b.snapshot().rate_limit_hits == 0
    assert gateway_b.snapshot().total_calls == 1
    assert sent_b[-1]["model"] == "qwen/qwen3.8-27b"


def test_gateway_requires_an_adapter_and_each_groq_adapter_owns_its_http_client():
    """A gateway can't silently fall back to shared state: it needs its own adapter."""
    from resilient_triage.llm import GroqAdapter, LLMGateway

    with pytest.raises(TypeError, match="adapter"):
        LLMGateway(primary="p", fallback="f", label="l")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="api_key"):
        GroqAdapter(timeout_s=5.0)  # type: ignore[call-arg]
    first, second = GroqAdapter("k", timeout_s=5.0), GroqAdapter("k", timeout_s=5.0)
    assert first._client is not second._client
