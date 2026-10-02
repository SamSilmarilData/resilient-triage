"""Tests for LangGraph cyclic state machine, self-repair loops, and resilience routing."""

import json
import uuid
import pytest
from langchain_core.messages import AIMessage

from resilient_triage.graph.builder import build_triage_graph
from resilient_triage.graph.llm import MockTriageChatModel, set_triage_llm
from resilient_triage.graph.nodes.analyze_query import analyze_query_node
from resilient_triage.graph.nodes.validate_report import clean_json_string
from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
from resilient_triage.schemas.incident import (
    DegradationStatus,
    IncidentTriageReport,
    SeverityLevel,
)


@pytest.fixture(autouse=True)
def reset_test_environment():
    """Reset registry and LLM overrides between tests."""
    circuit_breaker_registry.reset_all()
    set_triage_llm(None)
    yield
    circuit_breaker_registry.reset_all()
    set_triage_llm(None)


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
    raw_markdown = "```json\n{\"summary\": \"Test incident\"}\n```"
    assert clean_json_string(raw_markdown) == "{\"summary\": \"Test incident\"}"

    conversational = "Here is the incident triage report:\n\n{\"summary\": \"Outage\"}\n\nHope this helps!"
    assert clean_json_string(conversational) == "{\"summary\": \"Outage\"}"


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
