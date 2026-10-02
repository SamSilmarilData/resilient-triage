"""Telemetry and chaos data contracts."""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class StatuspageComponent(BaseModel):
    """Component representation from Atlassian Statuspage."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    status: str
    description: str | None = None


class StatuspageIncident(BaseModel):
    """Incident record from Atlassian Statuspage."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    status: str
    impact: str
    shortlink: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class StatuspageSummary(BaseModel):
    """Aggregated status summary from Atlassian Statuspage."""

    model_config = ConfigDict(extra="ignore")

    page_name: str = Field(..., description="Statuspage name")
    page_url: str = Field(default="", description="Statuspage URL")
    indicator: str = Field(..., description="Global indicator: none, minor, major, critical")
    description: str = Field(default="", description="Overall status description")
    components: list[StatuspageComponent] = Field(default_factory=list)
    incidents: list[StatuspageIncident] = Field(default_factory=list)


class ChaosConfig(BaseModel):
    """Runtime configuration for chaos simulation endpoint."""

    model_config = ConfigDict(extra="forbid")

    active: bool = Field(default=False, description="Whether chaos injection is enabled")
    inject_503: bool = Field(default=False, description="Always force 503 Service Unavailable")
    failure_rate: Annotated[float, Field(default=0.0, ge=0.0, le=1.0, description="Random failure rate (0.0 to 1.0)")]
    latency_ms: int = Field(default=0, ge=0, description="Artificial latency delay in milliseconds")


class ChaosTelemetryResponse(BaseModel):
    """Payload returned by the /chaos endpoint."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., description="Telemetry probe status: 'OK' or 'DEGRADED'")
    latency_injected_ms: int = Field(default=0, description="Latency delay introduced")
    error_injected: bool = Field(default=False, description="Whether an intentional fault was triggered")
    details: str = Field(..., description="Probe message or chaos diagnostic")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
