"""Node: finalize_report - Checks the report's system facts against what the run actually saw."""

from typing import Any

from resilient_triage.graph.reconciliation import DegradationEvidence, reconcile
from resilient_triage.schemas.state import TriageState


def finalize_node(state: TriageState) -> dict[str, Any]:
    """Reconcile the report's system facts; the Triage gateway stores it, not the graph."""
    report = state.get("final_report")
    if not report:
        return {}
    return {"final_report": reconcile(report, DegradationEvidence.from_state(state))}
