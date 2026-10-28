"""Node: fallback_report - Constructs deterministic safe report on retry exhaustion."""

from typing import Any

from resilient_triage.schemas.incident import (
    ActionRecommendation,
    IncidentTriageReport,
    PriorityLevel,
    SeverityLevel,
    TelemetrySignal,
)
from resilient_triage.schemas.state import TriageState


def fallback_report_node(state: TriageState) -> dict[str, Any]:
    """Emit guaranteed valid IncidentTriageReport when LLM self-repair retries are exhausted."""
    query = str(state.get("query") or state.get("incident_query") or "Incident investigation")
    validation_errors = state.get("validation_errors", [])

    raw_signals = state.get("telemetry_signals", [])
    signals = []
    for s in raw_signals:
        try:
            signals.append(TelemetrySignal.model_validate(s))
        except Exception:
            pass

    err_snippet = "; ".join(validation_errors[:2]) if validation_errors else "LLM synthesis error"
    if "429" in err_snippet:
        root_cause = "LLM provider rate limit reached (HTTP 429). Degraded heuristic fallback analysis engaged."
    elif "503" in err_snippet or "unavailable" in err_snippet.lower():
        root_cause = (
            "Upstream LLM provider temporarily unavailable. Safe heuristic fallback engaged."
        )
    else:
        root_cause = f"Self-repair limits reached. Diagnostic context: {err_snippet}"

    report = IncidentTriageReport(
        summary=f"Automated incident triage degraded for: {query[:80]}",
        severity=SeverityLevel.UNKNOWN,
        root_cause_analysis=root_cause,
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
        confidence_score=0.20,
    )

    return {
        "final_report": report,
        "is_degraded": True,
        "synthesis_fell_back": True,
    }
