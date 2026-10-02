"""LangGraph cyclic state graph schemas."""

from typing import Annotated, Any
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages

from resilient_triage.schemas.incident import IncidentTriageReport


class TriageState(TypedDict, total=False):
    """Execution state passed through the LangGraph cyclic state machine."""

    # User input
    query: str

    # Message history for agent nodes and self-repair loop
    messages: Annotated[list[Any], add_messages]

    # Telemetry payloads
    statuspage_telemetry: dict[str, Any] | None
    chaos_telemetry: dict[str, Any] | None

    # Resilience tracking
    circuit_breaker_status: dict[str, str]  # e.g. {"statuspage": "CLOSED", "chaos": "OPEN"}
    is_degraded: bool

    # Candidate and self-repair state
    candidate_report_raw: str | None
    repair_attempts: int
    validation_errors: list[str]

    # Validated final output
    final_report: IncidentTriageReport | None
