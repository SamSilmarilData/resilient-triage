"""Node: analyze_query - Normalizes the query and scope and picks the run's Targeted sources."""

from typing import Any

from langgraph.runtime import Runtime

from resilient_triage.graph.deps import TriageDeps
from resilient_triage.schemas.state import TriageState


def analyze_query_node(state: TriageState, runtime: Runtime[TriageDeps]) -> dict[str, Any]:
    """Normalize the query, ask the probe which sources to target, and reset per-run state."""
    raw_query = state.get("query") or state.get("incident_query") or ""
    query = str(raw_query).strip()
    service_filter = state.get("service_filter")
    targeted = runtime.context.telemetry.select_targets(query, service_filter)

    return {
        "query": query,
        "service_filter": service_filter,
        "targeted_sources": sorted(targeted),
        "messages": [],
        "telemetry_signals": [],
        "circuit_breaker_status": {},
        "tripped_breakers": [],
        "unavailable_sources": [],
        "synthesis_fell_back": False,
        "is_degraded": False,
        "degradation_reason": None,
        "historical_context": [],
        "repair_attempts": 0,
        "refinement_attempts": 0,
        "validation_errors": [],
        "provider_failure": None,
        "refined_from": None,
    }
