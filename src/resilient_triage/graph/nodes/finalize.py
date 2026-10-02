"""Node: finalize_report - Enforces consistency and commits report to semantic cache."""

from typing import Any

from resilient_triage.cache.semantic_cache import semantic_cache
from resilient_triage.schemas.incident import DegradationStatus
from resilient_triage.schemas.state import TriageState


async def finalize_node(state: TriageState) -> dict[str, Any]:
    """Finalize report metadata and persist to semantic cache."""
    report = state.get("final_report")
    if not report:
        return {}

    cb_status = state.get("circuit_breaker_status", {})
    tripped = [name for name, st in cb_status.items() if st == "open"]

    # Programmatically enforce circuit breaker consistency
    if tripped:
        report.circuit_breakers_tripped = list(set(report.circuit_breakers_tripped + tripped))
        if len(tripped) >= 2:
            report.degradation_status = DegradationStatus.SEVERELY_DEGRADED
        else:
            report.degradation_status = DegradationStatus.PARTIALLY_DEGRADED

    # Store validated report in semantic cache (<2ms)
    query = state.get("query", "")
    service_filter = state.get("service_filter")
    await semantic_cache.store(query=query, report=report, service_filter=service_filter)

    return {
        "final_report": report,
    }
