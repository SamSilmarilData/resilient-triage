"""Node: fallback_report - Constructs deterministic safe report on retry exhaustion."""

from typing import Any

from resilient_triage.schemas.incident import (
    ActionRecommendation,
    DegradationStatus,
    IncidentTriageReport,
    PriorityLevel,
    SeverityLevel,
    TelemetrySignal,
)
from resilient_triage.schemas.state import TriageState


def fallback_report_node(state: TriageState) -> dict[str, Any]:
    """Emit guaranteed valid IncidentTriageReport when LLM self-repair retries are exhausted."""
    query = state.get("query", "Incident investigation")
    validation_errors = state.get("validation_errors", [])
    cb_status = state.get("circuit_breaker_status", {})
    tripped = [name for name, st in cb_status.items() if st == "open"]

    raw_signals = state.get("telemetry_signals", [])
    signals = []
    for s in raw_signals:
        try:
            signals.append(TelemetrySignal.model_validate(s))
        except Exception:
            pass

    report = IncidentTriageReport(
        summary=f"Automated incident triage degraded for: {query[:80]}",
        severity=SeverityLevel.UNKNOWN,
        root_cause_analysis=(
            f"Self-repair retries exhausted after 2 attempts. "
            f"Validation issues encountered: {'; '.join(validation_errors[:2])}"
        ),
        affected_services=[],
        telemetry_signals=signals,
        recommended_actions=[
            ActionRecommendation(
                priority=PriorityLevel.P0,
                action="Manually inspect service dashboards and verify telemetry signals",
                target_system="Monitoring",
                action_type="diagnostic",
                requires_approval=False,
            )
        ],
        degradation_status=DegradationStatus.SEVERELY_DEGRADED,
        circuit_breakers_tripped=tripped,
        confidence_score=0.20,
    )

    return {
        "final_report": report,
        "is_degraded": True,
    }
