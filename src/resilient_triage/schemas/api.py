"""FastAPI request and response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.telemetry import ChaosConfig
from resilient_triage.trace.summary import TraceSummary


class TriageRequest(BaseModel):
    """User request to triage an ongoing or suspected incident."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="Incident description, symptoms, or triage question",
        examples=["GitHub Actions workflows failing with 503 and high webhook latency"],
    )
    service_filter: str | None = Field(
        default=None,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_\-\s]+$",
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
    cached: bool = Field(
        default=False, description="True if served from semantic cache (X-Cache: HIT)"
    )
    cache_similarity: float | None = Field(
        default=None,
        description="Cosine similarity score if matched in semantic cache",
    )
    execution_time_ms: float = Field(
        ...,
        description="Total round-trip time in milliseconds (sub-20ms target for cache hits)",
    )
    trace: TraceSummary | None = Field(
        default=None,
        description=(
            "Structured summary of this request's execution trace (ADR-008): node path, "
            "routes, model calls, telemetry reads, reconciliation and timings. Never the "
            "event list. None when the request was not traced."
        ),
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


class LLMQuotaSnapshot(BaseModel):
    """Real-time quota, rate limit, and failover telemetry for active LLM provider."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="groq", description="LLM provider name")
    active_model: str = Field(..., description="Active primary model name")
    fallback_model: str | None = Field(default=None, description="Active fallback model name")
    remaining_requests: int | None = Field(
        default=None, description="Requests remaining in current window"
    )
    limit_requests: int | None = Field(
        default=None, description="Total request limit for current window"
    )
    remaining_tokens: int | None = Field(
        default=None, description="Tokens remaining in current window"
    )
    limit_tokens: int | None = Field(
        default=None, description="Total token limit for current window"
    )
    reset_requests: str | None = Field(
        default=None, description="Time until request quota window resets"
    )
    reset_tokens: str | None = Field(
        default=None, description="Time until token quota window resets"
    )
    total_calls: int = Field(default=0, description="Total LLM API calls executed")
    rate_limit_hits: int = Field(
        default=0, description="Total HTTP 429 rate limit events encountered"
    )
    failover_events: int = Field(
        default=0, description="Total successful fallback model invocations"
    )
    last_status: int | None = Field(
        default=None, description="HTTP status code of most recent LLM response"
    )
    last_error: str | None = Field(
        default=None, description="Most recent error message or rate limit description"
    )
    last_updated: datetime | None = Field(
        default=None, description="Timestamp of most recent update"
    )


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
        description=(
            "Current state of all registered circuit breakers ('closed', 'open', 'half-open')"
        )
    )
    active_model: str = Field(
        default="Zero-Config SRE Simulation",
        description="Name of currently active LLM inference engine",
    )
    llm_quota: LLMQuotaSnapshot | None = Field(
        default=None,
        description="Active LLM quota, token budget, and failover metrics snapshot",
    )


class ResilienceStatusResponse(BaseModel):
    """Deep resilience diagnostics for circuit breakers, chaos, cache, and LLM quotas."""

    model_config = ConfigDict(extra="forbid")

    circuits: dict[str, CircuitSnapshot]
    chaos_active: bool
    cache_driver: str
    cache_entries_count: int
    active_model: str = Field(
        default="Zero-Config SRE Simulation",
        description="Name of currently active LLM inference engine",
    )
    llm_quota: LLMQuotaSnapshot | None = Field(
        default=None,
        description="Active LLM quota and rate limit telemetry snapshot",
    )


class ScenarioView(BaseModel):
    """What the page may see of a scenario: its copy, never its script or expected outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    chapter: str = Field(description="Id of the chapter the scenario is shown under")
    title: str
    caption: str
    setup_notes: tuple[str, ...] = Field(
        description="What is true before the run, in words: its setup, overrides and faults"
    )


class ScenarioChapterView(BaseModel):
    """A chapter of the scenario catalogue, with its scenarios in display order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    scenarios: list[ScenarioView]


class ScenarioCatalogue(BaseModel):
    """GET /scenarios: the public catalogue, chapters and their scenarios in display order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chapters: list[ScenarioChapterView]
