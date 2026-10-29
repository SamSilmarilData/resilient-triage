"""Graph nodes read their collaborators from the LangGraph run context (TriageDeps)."""

import json

from fakes import FakeHistory, make_triage_deps
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from resilient_triage.cache.semantic_cache import CacheMatch
from resilient_triage.graph.nodes.collect_telemetry import collect_telemetry_node
from resilient_triage.graph.nodes.compensate_blindspots import compensate_blindspots_node
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
        {"query": "Service outage", "target_tools": ["statuspage", "chaos"]},
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


async def test_finalize_stores_the_report_through_the_context_cache_writer():
    history = FakeHistory()

    result = await finalize_node(
        {"query": "Payments API failing", "service_filter": "billing", "final_report": _report()},
        Runtime(context=make_triage_deps(history=history)),
    )

    assert len(history.stored) == 1
    query, stored_report, service_filter = history.stored[0]
    assert (query, service_filter) == ("Payments API failing", "billing")
    assert stored_report == result["final_report"]
