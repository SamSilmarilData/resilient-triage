"""Node: compensate_blindspots - Compensates for tripped circuit breakers with historical cache context."""

from typing import Any

from resilient_triage.cache.semantic_cache import semantic_cache
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.state import TriageState


async def compensate_blindspots_node(state: TriageState) -> dict[str, Any]:
    """Query semantic cache to bridge blind spots left by offline or tripped telemetry sources."""
    query = state.get("query", "")
    service_filter = state.get("service_filter")
    existing_signals = list(state.get("telemetry_signals", []))
    historical_context: list[str] = []

    # Attempt to retrieve related historical incidents from cache (lower similarity threshold 0.70 for historical breadth)
    match = await semantic_cache.lookup(query=query, service_filter=service_filter, threshold=0.70)
    if match:
        snippet = f"Previous incident on '{match.matched_query}': Root cause: {match.report.root_cause_analysis}. Severity: {match.report.severity}."
        historical_context.append(snippet)
        existing_signals.append(
            TelemetrySignal(
                source="cache_compensation",
                status="HISTORICAL_PATTERN_ATTACHED",
                raw_snippet=f"Compensating for offline telemetry: {snippet}",
            ).model_dump()
        )
    else:
        historical_context.append(
            f"No prior incident match found in semantic cache for scope '{service_filter or 'general'}'. Proceeding with baseline heuristics."
        )

    return {
        "historical_context": historical_context,
        "telemetry_signals": existing_signals,
    }
