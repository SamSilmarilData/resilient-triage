"""Report reconciliation - makes a report's system facts agree with the run's evidence."""

import logging
from dataclasses import dataclass

from resilient_triage.schemas.incident import (
    ActionRecommendation,
    DegradationStatus,
    IncidentTriageReport,
)
from resilient_triage.schemas.state import TriageState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DegradationEvidence:
    """Facts about a run that determine a report's degradation status."""

    tripped_breakers: frozenset[str]
    unavailable_sources: frozenset[str]
    synthesis_fell_back: bool

    @classmethod
    def from_state(cls, state: TriageState) -> "DegradationEvidence":
        """Build the evidence recorded in the run's state."""
        return cls(
            tripped_breakers=frozenset(state.get("tripped_breakers", [])),
            unavailable_sources=frozenset(state.get("unavailable_sources", [])),
            synthesis_fell_back=bool(state.get("synthesis_fell_back", False)),
        )


def _degradation_status(evidence: DegradationEvidence) -> DegradationStatus:
    """Return the degradation status implied by the evidence."""
    if evidence.synthesis_fell_back or len(evidence.tripped_breakers) >= 2:
        return DegradationStatus.SEVERELY_DEGRADED
    if len(evidence.tripped_breakers) == 1:
        return DegradationStatus.PARTIALLY_DEGRADED
    return DegradationStatus.HEALTHY


def _with_approval(action: ActionRecommendation) -> ActionRecommendation:
    """Return the action with requires_approval raised unless it is diagnostic."""
    if action.requires_approval or action.action_type.strip().lower() == "diagnostic":
        return action
    logger.warning(
        "Approval rule enforced: requires_approval=True on %s action '%s'",
        action.action_type,
        action.action,
    )
    return action.model_copy(update={"requires_approval": True})


def reconcile(report: IncidentTriageReport, evidence: DegradationEvidence) -> IncidentTriageReport:
    """Return a copy of report whose system facts match the evidence and approval rule."""
    return report.model_copy(
        update={
            "circuit_breakers_tripped": sorted(evidence.tripped_breakers),
            "degradation_status": _degradation_status(evidence),
            "recommended_actions": [_with_approval(a) for a in report.recommended_actions],
        }
    )
