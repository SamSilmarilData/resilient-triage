"""FastAPI request and response schemas."""

from pydantic import BaseModel, ConfigDict, Field
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.telemetry import ChaosConfig


class TriageRequest(BaseModel):
    """User request to triage an ongoing or suspected incident."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=3,
        description="Incident description, symptoms, or triage question",
        examples=["GitHub Actions workflows failing with 503 and high webhook latency"],
    )
    service_filter: str | None = Field(
        default=None,
        description="Optional filter for specific service component",
    )
    force_refresh: bool = Field(
        default=False,
        description="Bypass semantic cache lookup if set to True",
    )


class TriageResponse(BaseModel):
    """API response containing the validated report and caching telemetry."""

    model_config = ConfigDict(extra="forbid")

    report: IncidentTriageReport
    cached: bool = Field(default=False, description="True if served from semantic cache (X-Cache: HIT)")
    cache_similarity: float | None = Field(
        default=None,
        description="Cosine similarity score if matched in semantic cache",
    )
    execution_time_ms: float = Field(
        ...,
        description="Total round-trip time in milliseconds (sub-20ms target for cache hits)",
    )


class ChaosTriggerRequest(ChaosConfig):
    """Request body to update runtime chaos simulation parameters."""
    pass
