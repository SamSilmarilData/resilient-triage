"""FastAPI request and response schemas."""

from datetime import datetime
from typing import Literal

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


class CircuitSnapshot(BaseModel):
    """Snapshot representation of a circuit breaker."""

    model_config = ConfigDict(extra="forbid")

    name: str
    state: str = Field(..., description="Current state: 'closed', 'open', or 'half-open'")
    fail_counter: int = Field(..., description="Consecutive failure count")
    fail_max: int = Field(..., description="Failure threshold to trip breaker")
    is_open: bool = Field(..., description="True if breaker is currently open")
    last_failure_time: datetime | None = None
    last_state_change: datetime | None = None
    last_failure_reason: str | None = None


class HealthResponse(BaseModel):
    """System health check and component availability status."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["healthy", "degraded"]
    version: str
    redis_connected: bool
    cache_driver: str = Field(
        default="in_memory",
        description="Active cache driver: 'redisearch', 'redis_hash_fallback', or 'in_memory'",
    )
    circuits: dict[str, str] = Field(
        description="Current state of all registered circuit breakers ('closed', 'open', 'half-open')"
    )
    active_model: str = Field(
        default="Zero-Config SRE Simulation",
        description="Name of currently active LLM inference engine",
    )


class ResilienceStatusResponse(BaseModel):
    """Deep resilience diagnostics for circuit breakers, chaos, and cache."""

    model_config = ConfigDict(extra="forbid")

    circuits: dict[str, CircuitSnapshot]
    chaos_active: bool
    cache_driver: str
    cache_entries_count: int
    active_model: str = Field(
        default="Zero-Config SRE Simulation",
        description="Name of currently active LLM inference engine",
    )

