"""Node: analyze_query - Extracts intent, normalizes scope, and targets relevant telemetry."""

from typing import Any
from langchain_core.messages import HumanMessage

from resilient_triage.schemas.state import TriageState

EXTERNAL_KEYWORDS = {"github", "statuspage", "actions", "repo", "webhook", "cloud", "aws", "gcp", "atlassian"}
INTERNAL_KEYWORDS = {"internal", "chaos", "auth", "database", "redis", "latency probe", "payment", "chaos_probe"}


def analyze_query_node(state: TriageState) -> dict[str, Any]:
    """Inspect query context and dynamically target relevant telemetry sources."""
    query = state.get("query", "").strip()
    service_filter = state.get("service_filter")
    combined_text = f"{query} {service_filter or ''}".lower()

    # Dynamic tool targeting
    has_external = any(kw in combined_text for kw in EXTERNAL_KEYWORDS)
    has_internal = any(kw in combined_text for kw in INTERNAL_KEYWORDS)

    if has_external and not has_internal:
        target_tools = ["statuspage"]
    elif has_internal and not has_external:
        target_tools = ["chaos"]
    else:
        target_tools = ["statuspage", "chaos"]

    messages = list(state.get("messages", []))
    if not messages:
        messages = [HumanMessage(content=f"Triage request: {query}")]

    return {
        "query": query,
        "service_filter": service_filter,
        "target_tools": target_tools,
        "messages": messages,
        "telemetry_signals": [],
        "circuit_breaker_status": {},
        "is_degraded": False,
        "degradation_reason": None,
        "historical_context": [],
        "repair_attempts": 0,
        "refinement_attempts": 0,
        "validation_errors": [],
    }
