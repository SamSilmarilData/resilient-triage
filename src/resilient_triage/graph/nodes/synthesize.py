"""Node: synthesize_telemetry - Consolidates signals, circuit state, and warnings into model prompt."""

from typing import Any
from langchain_core.messages import HumanMessage, SystemMessage

from resilient_triage.graph.prompts import (
    TRIAGE_SYSTEM_PROMPT,
    format_telemetry_prompt,
)
from resilient_triage.schemas.state import TriageState


def synthesize_telemetry_node(state: TriageState) -> dict[str, Any]:
    """Assemble structured telemetry context for report generation."""
    query = state.get("query", "")
    signals = state.get("telemetry_signals", [])
    cb_status = state.get("circuit_breaker_status", {})
    degradation_reason = state.get("degradation_reason")
    historical_context = state.get("historical_context", [])

    prompt_text = format_telemetry_prompt(
        query=query,
        signals=signals,
        circuit_status=cb_status,
        degradation_note=degradation_reason,
        historical_context=historical_context,
    )

    messages = [SystemMessage(content=TRIAGE_SYSTEM_PROMPT), HumanMessage(content=prompt_text)]

    return {
        "messages": messages,
    }
