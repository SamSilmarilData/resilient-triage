"""Node: finalize_report - Reconciles the report with run evidence and caches it."""

from typing import Any

from resilient_triage.cache.semantic_cache import semantic_cache
from resilient_triage.graph.reconciliation import DegradationEvidence, reconcile
from resilient_triage.schemas.state import TriageState


async def finalize_node(state: TriageState) -> dict[str, Any]:
    """Reconcile the report's system facts and persist it to the semantic cache."""
    report = state.get("final_report")
    if not report:
        return {}

    report = reconcile(report, DegradationEvidence.from_state(state))

    # Store validated report in semantic cache (<2ms)
    query = state.get("query", "")
    service_filter = state.get("service_filter")
    await semantic_cache.store(query=query, report=report, service_filter=service_filter)

    return {
        "final_report": report,
    }
