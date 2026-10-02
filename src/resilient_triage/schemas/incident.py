"""Incident triage report data contracts and enums."""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class SeverityLevel(str, Enum):
    """Incident severity classification."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class DegradationStatus(str, Enum):
    """Internal triage agent degradation state."""

    HEALTHY = "HEALTHY"
    PARTIALLY_DEGRADED = "PARTIALLY_DEGRADED"
    SEVERELY_DEGRADED = "SEVERELY_DEGRADED"
    UNKNOWN = "UNKNOWN"


class PriorityLevel(str, Enum):
    """Action priority rating."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class ServiceImpact(BaseModel):
    """Specific impacted upstream or internal service."""

    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(..., description="Name of the affected service or component")
    impact_level: str = Field(..., description="Qualitative impact (e.g. Total Outage, Latency Spike, Intermittent 503s)")
    details: str = Field(..., description="Observed symptoms or relevant context")


class TelemetrySignal(BaseModel):
    """Signal collected from telemetry inputs during triage."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., description="Telemetry source identifier (e.g., statuspage, chaos_probe)")
    status: str = Field(..., description="Operational status observed")
    latency_ms: float | None = Field(default=None, description="Observed round-trip latency in ms")
    error_rate: float | None = Field(default=None, description="Observed error rate fraction (0.0 - 1.0)")
    raw_snippet: str | None = Field(default=None, description="Short snippet of raw telemetry data or status message")


class ActionRecommendation(BaseModel):
    """Actionable mitigation step for on-call engineers."""

    model_config = ConfigDict(extra="forbid")

    priority: PriorityLevel = Field(default=PriorityLevel.P1, description="Action urgency priority")
    action: str = Field(..., description="Concrete action command or instruction")
    target_system: str = Field(..., description="Target service or infrastructure component")
    action_type: str = Field(
        default="remediative",
        description="Action nature: 'diagnostic' (read-only/safe) or 'remediative' (mutating/mitigating)",
    )
    requires_approval: bool = Field(
        default=False,
        description="True if command is high-risk or mutating and requires approval",
    )


class IncidentTriageReport(BaseModel):
    """Strictly validated incident triage report output."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier for the triage incident report",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of report generation",
    )
    summary: str = Field(..., min_length=5, description="Executive summary of the incident")
    severity: SeverityLevel = Field(..., description="Incident severity level")
    root_cause_analysis: str = Field(..., min_length=5, description="Inferred root cause or hypothesis")
    affected_services: list[ServiceImpact] = Field(
        default_factory=list,
        description="List of impacted services and degradation levels",
    )
    telemetry_signals: list[TelemetrySignal] = Field(
        default_factory=list,
        description="Signals gathered from external status pages and internal chaos probes",
    )
    recommended_actions: list[ActionRecommendation] = Field(
        default_factory=list,
        description="Prioritized remediation steps",
    )
    degradation_status: DegradationStatus = Field(
        default=DegradationStatus.HEALTHY,
        description="Agent operational mode during triage (HEALTHY, PARTIALLY_DEGRADED, SEVERELY_DEGRADED)",
    )
    circuit_breakers_tripped: list[str] = Field(
        default_factory=list,
        description="Names of circuit breakers that tripped open during execution",
    )
    confidence_score: Annotated[float, Field(ge=0.0, le=1.0, description="Model confidence score between 0.0 and 1.0")]
