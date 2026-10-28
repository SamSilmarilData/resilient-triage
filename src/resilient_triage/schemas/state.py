"""LangGraph cyclic state graph schemas."""

from typing import Annotated, Any

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from resilient_triage.schemas.incident import IncidentTriageReport


class TriageState(TypedDict, total=False):
    """Execution state passed through the LangGraph cyclic state machine."""

    # User input and scope
    query: str
    service_filter: str | None

    # Dynamic tool targeting
    target_tools: list[str]

    # Message history for agent nodes and self-repair loop
    messages: Annotated[list[Any], add_messages]

    # Telemetry payloads and signals
    statuspage_telemetry: dict[str, Any] | None
    chaos_telemetry: dict[str, Any] | None
    telemetry_signals: list[dict[str, Any]]

    # Resilience tracking & compensatory history
    circuit_breaker_status: dict[str, str]  # e.g. {"statuspage": "CLOSED", "chaos": "OPEN"}
    tripped_breakers: list[str]  # targeted tools whose breaker was open at collection
    unavailable_sources: list[str]  # targeted tools that could not be read (populated later)
    synthesis_fell_back: bool  # report was built by fallback_report
    is_degraded: bool
    degradation_reason: str | None
    historical_context: list[str]

    # Candidate and self-repair state
    candidate_report_raw: str | None
    repair_attempts: int
    refinement_attempts: int
    validation_errors: list[str]

    # Validated final output
    final_report: IncidentTriageReport | None
