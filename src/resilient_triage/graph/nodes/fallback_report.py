"""Node: fallback_report - Constructs a safe report on a provider failure or repair exhaustion."""

from collections.abc import Sequence
from typing import Any

from resilient_triage.llm.failures import (
    MalformedResponse,
    ProviderFailure,
    RateLimited,
    Rejected,
    Unavailable,
)
from resilient_triage.schemas.incident import (
    ActionRecommendation,
    IncidentTriageReport,
    PriorityLevel,
    SeverityLevel,
    TelemetrySignal,
)
from resilient_triage.schemas.state import TriageState


def fallback_reason(failure: ProviderFailure | None, validation_errors: list[str]) -> str:
    """Explain the fallback by the provider failure kind, or by exhausted self-repair."""
    match failure:
        case RateLimited():
            return (
                "LLM provider rate limit reached (HTTP 429). Degraded heuristic fallback analysis "
                "engaged."
            )
        case Unavailable() | MalformedResponse():
            return "Upstream LLM provider temporarily unavailable. Safe heuristic fallback engaged."
        case Rejected():
            return "LLM provider rejected the request. Safe heuristic fallback engaged."
        case None:
            err_snippet = (
                "; ".join(validation_errors[:2]) if validation_errors else "LLM synthesis error"
            )
            return f"Self-repair limits reached. Diagnostic context: {err_snippet}"


def build_fallback_report(
    query: str, *, reason: str, signals: Sequence[TelemetrySignal] = ()
) -> IncidentTriageReport:
    """Build a guaranteed valid low-confidence report whose root cause is ``reason``."""
    return IncidentTriageReport(
        summary=f"Automated incident triage degraded for: {query[:80]}",
        severity=SeverityLevel.UNKNOWN,
        root_cause_analysis=reason,
        affected_services=[],
        telemetry_signals=list(signals),
        recommended_actions=[
            ActionRecommendation(
                priority=PriorityLevel.P0,
                action="Manually inspect service dashboards and verify telemetry signals",
                target_system="Monitoring",
                action_type="diagnostic",
            )
        ],
        confidence_score=0.20,
    )


def valid_signals(raw_signals: Sequence[Any]) -> list[TelemetrySignal]:
    """Keep the telemetry signals that validate, dropping malformed ones."""
    signals = []
    for s in raw_signals:
        try:
            signals.append(TelemetrySignal.model_validate(s))
        except Exception:
            pass
    return signals


def fallback_report_node(state: TriageState) -> dict[str, Any]:
    """Emit a guaranteed valid report after a provider failure or exhausted self-repair."""
    query = str(state.get("query") or state.get("incident_query") or "Incident investigation")
    reason = fallback_reason(state.get("provider_failure"), state.get("validation_errors", []))
    signals = valid_signals(state.get("telemetry_signals", []))

    return {
        "final_report": build_fallback_report(query, reason=reason, signals=signals),
        "is_degraded": True,
        "synthesis_fell_back": True,
    }
