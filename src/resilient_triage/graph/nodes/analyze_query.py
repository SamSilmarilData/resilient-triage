"""Node: analyze_query - Extracts intent, normalizes scope, and targets relevant telemetry."""

import re
from typing import Any

from resilient_triage.schemas.state import TriageState

EXTERNAL_KEYWORDS = {
    "github",
    "statuspage",
    "actions",
    "repo",
    "webhook",
    "cloud",
    "aws",
    "gcp",
    "atlassian",
}
INTERNAL_KEYWORDS = {
    "internal",
    "chaos",
    "auth",
    "database",
    "redis",
    "latency probe",
    "payment",
    "chaos_probe",
}


def _whole_word_pattern(keywords: set[str]) -> re.Pattern[str]:
    # \b treats "_" as a word character and "-" as a boundary, so "auth" matches
    # "auth-service" but not "author" or "my_auth_helper".
    alternatives = "|".join(re.escape(kw) for kw in sorted(keywords))
    return re.compile(rf"\b(?:{alternatives})\b")


_EXTERNAL_PATTERN = _whole_word_pattern(EXTERNAL_KEYWORDS)
_INTERNAL_PATTERN = _whole_word_pattern(INTERNAL_KEYWORDS)


def analyze_query_node(state: TriageState) -> dict[str, Any]:
    """Inspect query context and dynamically target relevant telemetry sources."""
    raw_query = state.get("query") or state.get("incident_query") or ""
    query = str(raw_query).strip()
    service_filter = state.get("service_filter")
    combined_text = f"{query} {service_filter or ''}".lower()

    # Dynamic tool targeting: keywords count only as whole words or phrases
    has_external = _EXTERNAL_PATTERN.search(combined_text) is not None
    has_internal = _INTERNAL_PATTERN.search(combined_text) is not None

    if has_external and not has_internal:
        target_tools = ["statuspage"]
    elif has_internal and not has_external:
        target_tools = ["chaos"]
    else:
        target_tools = ["statuspage", "chaos"]

    return {
        "query": query,
        "service_filter": service_filter,
        "target_tools": target_tools,
        "messages": [],
        "telemetry_signals": [],
        "circuit_breaker_status": {},
        "tripped_breakers": [],
        "unavailable_sources": [],
        "synthesis_fell_back": False,
        "is_degraded": False,
        "degradation_reason": None,
        "historical_context": [],
        "repair_attempts": 0,
        "refinement_attempts": 0,
        "validation_errors": [],
        "provider_failure": None,
        "refined_from": None,
    }
