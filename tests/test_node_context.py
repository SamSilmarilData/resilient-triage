"""Graph nodes read their collaborators from the LangGraph run context (TriageDeps)."""

import json

from fakes import FakeHistory, make_triage_deps
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from resilient_triage.cache.semantic_cache import CacheMatch
from resilient_triage.graph.nodes.collect_telemetry import collect_telemetry_node
from resilient_triage.graph.nodes.compensate_blindspots import compensate_blindspots_node
from resilient_triage.graph.nodes.degraded_fallback import degraded_fallback_node
from resilient_triage.graph.nodes.finalize import finalize_node
from resilient_triage.graph.nodes.generate_report import generate_report_node
from resilient_triage.llm import ScriptedAdapter
from resilient_triage.resilience.circuit_breaker import force_open_for_tests
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport, SeverityLevel


def _report() -> IncidentTriageReport:
    return IncidentTriageReport(
        summary="Payments API returning 503",
        severity=SeverityLevel.HIGH,
        root_cause_analysis="Upstream payment processor outage",
        degradation_status=DegradationStatus.HEALTHY,
        circuit_breakers_tripped=[],
        confidence_score=0.9,
    )


async def test_collect_telemetry_reads_statuspage_and_breakers_from_context():
    deps = make_triage_deps()
    force_open_for_tests(deps.telemetry.breakers.get_or_create("chaos"))

    result = await collect_telemetry_node(
        {"query": "Service outage", "targeted_sources": ["statuspage", "chaos"]},
        Runtime(context=deps),
    )

    sources = [signal["source"] for signal in result["telemetry_signals"]]
    assert "statuspage:global" in sources
    assert result["tripped_breakers"] == ["chaos"]
    assert result["unavailable_sources"] == []


async def test_compensate_blindspots_reads_history_from_context():
    match = CacheMatch(
        report=_report(),
        similarity=0.8,
        execution_time_ms=1.0,
        matched_query="Payments API down last week",
    )
    history = FakeHistory(match=match)

    result = await compensate_blindspots_node(
        {"query": "Payments API failing", "service_filter": "billing"},
        Runtime(context=make_triage_deps(history=history)),
    )

    assert history.lookups == [("Payments API failing", "billing", 0.70)]
    assert "Payments API down last week" in result["historical_context"][0]
    assert result["telemetry_signals"][-1]["source"] == "cache_compensation"


async def test_generate_report_calls_the_llm_from_context():
    llm = ScriptedAdapter([json.dumps({"summary": "from the context llm"})])

    result = await generate_report_node(
        {"messages": [HumanMessage(content="Is checkout down?")]},
        Runtime(context=make_triage_deps(llm_adapter=llm)),
    )

    assert result["candidate_report_raw"] == json.dumps({"summary": "from the context llm"})
    assert len(llm.calls) == 1


def test_finalize_reconciles_the_report_without_any_context():
    # The Triage gateway stores reports now; finalize needs no collaborator at all.
    result = finalize_node(
        {
            "query": "Payments API failing",
            "service_filter": "billing",
            "final_report": _report(),
            "tripped_breakers": ["statuspage"],
        }
    )

    report = result["final_report"]
    assert report.circuit_breakers_tripped == ["statuspage"]
    assert report.degradation_status == DegradationStatus.PARTIALLY_DEGRADED


def test_degraded_fallback_names_tripped_breakers_by_their_source_display_name():
    """The degradation reason names each tripped breaker the way its Telemetry source does."""
    result = degraded_fallback_node(
        {"tripped_breakers": ["chaos", "statuspage"], "telemetry_signals": []},
        Runtime(context=make_triage_deps()),
    )

    reason = result["degradation_reason"]
    assert "[Cluster Sentinel [chaos], Statuspage [statuspage]]" in reason
    assert result["is_degraded"] is True
    assert result["telemetry_signals"][-1]["raw_snippet"] == reason
