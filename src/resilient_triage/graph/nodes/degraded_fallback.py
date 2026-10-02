"""Node: degraded_fallback - Handles tripped circuit breakers and marks degradation."""

from typing import Any

from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.state import TriageState


def degraded_fallback_node(state: TriageState) -> dict[str, Any]:
    """Annotate state when one or more circuit breakers are OPEN."""
    cb_status = state.get("circuit_breaker_status", {})
    tripped = [name for name, st in cb_status.items() if st == "open"]
    signals = list(state.get("telemetry_signals", []))

    degradation_reason = (
        f"Circuit breaker(s) [{', '.join(tripped)}] tripped OPEN. Upstream telemetry unavailable."
        if tripped
        else "Upstream telemetry unavailable due to failures."
    )

    signals.append(
        TelemetrySignal(
            source="resilience_guard",
            status="CIRCUIT_BREAKER_OPEN",
            error_rate=1.0,
            raw_snippet=degradation_reason,
        ).model_dump()
    )

    return {
        "is_degraded": True,
        "degradation_reason": degradation_reason,
        "telemetry_signals": signals,
    }
