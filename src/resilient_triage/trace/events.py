"""Trace events: the frozen, free-text-free records an execution trace is made of (ADR-008).

Every event is a frozen Pydantic model in one discriminated union on `kind`. Fields are ids,
Literals, enums, counts and timings only: no query text, prompt, model output, signal snippet
or Pydantic error `input` can travel in one. The report travels once, in `trace_finished`.

`seq` and `t_ms` are stamped by the recorder (`trace.recorder`); callers leave them at 0.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from resilient_triage.llm.failures import RateLimitScope
from resilient_triage.schemas.api import TriageResponse
from resilient_triage.schemas.incident import DegradationStatus

# Bumped only when a kind or field is renamed or removed; adding either is not a bump.
TRACE_SCHEMA_VERSION = 1

# A code-defined name (node, route key, source, model, field, error type, scenario): never
# free text. The pattern keeps whitespace and prose out of every id on the wire.
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
Milliseconds = Annotated[float, Field(ge=0)]

CacheBackend = Literal["redisearch", "redis_hash_fallback", "in_memory"]
BreakerState = Literal["closed", "open", "half-open"]
ModelRole = Literal["primary", "fallback"]
ModelOutcome = Literal[
    "completion", "rate_limited", "unavailable", "rejected", "malformed_response"
]
TriageOutcomeKind = Literal["cache_hit", "analyzed", "shed"]


class _TraceEvent(BaseModel):
    """Fields every trace event has; `seq` and `t_ms` are stamped by the recorder."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(default=0, ge=0, description="Position in the trace, from 0")
    t_ms: float = Field(
        default=0.0, ge=0, description="Monotonic milliseconds since the trace's recorder began"
    )


class TraceStarted(_TraceEvent):
    """The request reached the triage gateway (scenario setup, if any, comes before it)."""

    kind: Literal["trace_started"] = "trace_started"
    schema_version: int = Field(default=TRACE_SCHEMA_VERSION, ge=1)
    scenario_id: Identifier | None = Field(
        default=None, description="Catalogue scenario id for a scenario run, else None"
    )
    scoped: bool = Field(default=False, description="True if the request had a service filter")
    force_refresh: bool = Field(default=False, description="True if the cache was bypassed")


class ScenarioSetup(_TraceEvent):
    """One step a scenario run took before the request, drawn before t = 0."""

    kind: Literal["scenario_setup"] = "scenario_setup"
    step: Literal["seed_cache", "hold_run_slots", "fail_source"]
    count: int = Field(ge=0, description="Entries seeded, slots held, or failed reads forced")
    source: Identifier | None = Field(default=None, description="Source id for fail_source")


class CacheLookup(_TraceEvent):
    """The semantic cache was asked for a similar answered query."""

    kind: Literal["cache_lookup"] = "cache_lookup"
    backend: CacheBackend
    hit: bool
    similarity: float | None = Field(default=None, ge=-1, le=1, description="Set on a hit")
    threshold: float = Field(ge=0, le=1)
    duration_ms: Milliseconds


class RunSlot(_TraceEvent):
    """The request waited for a run slot, and got one or was shed."""

    kind: Literal["run_slot"] = "run_slot"
    acquired: bool
    waited_ms: Milliseconds
    capacity: int = Field(ge=1, description="Run slots in total")


class NodeStarted(_TraceEvent):
    """A graph node began; each visit to a node is its own pair of events."""

    kind: Literal["node_started"] = "node_started"
    node: Identifier


class NodeFinished(_TraceEvent):
    """A graph node ended."""

    kind: Literal["node_finished"] = "node_finished"
    node: Identifier
    duration_ms: Milliseconds
    status: Literal["ok", "error", "cancelled"] = "ok"
    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="validate_report only: the valid report's confidence (the router's input)",
    )


class RouteDecided(_TraceEvent):
    """A conditional edge was taken; derived from the (source, target) pair's edge label."""

    kind: Literal["route_decided"] = "route_decided"
    source: Identifier
    target: Identifier
    route: Identifier = Field(description="The router's key, e.g. 'repair' or 'compensate'")


class ModelCall(_TraceEvent):
    """One adapter attempt inside one LLMGateway.complete()."""

    kind: Literal["model_call"] = "model_call"
    model: Identifier
    role: ModelRole
    outcome: ModelOutcome
    rate_limit_scope: RateLimitScope | None = Field(default=None, description="Set on a 429")
    retry_after_s: float | None = Field(default=None, ge=0, description="Set on a 429")
    duration_ms: Milliseconds


class TelemetryRead(_TraceEvent):
    """One guarded read of one Telemetry source."""

    kind: Literal["telemetry_read"] = "telemetry_read"
    source: Identifier
    phase: Literal["setup"] | None = Field(
        default=None, description="'setup' for a scenario's pre-run read, else None"
    )
    outcome: Literal["read", "tripped", "unavailable"]
    attempts: int = Field(ge=0, description="Read attempts made; 0 if the breaker refused")
    breaker_before: BreakerState
    breaker_after: BreakerState
    signals: int = Field(ge=0, description="Signals the read added")
    duration_ms: Milliseconds


class ValidationFailed(_TraceEvent):
    """A candidate report failed validation (names of fields, never their values)."""

    kind: Literal["validation_failed"] = "validation_failed"
    attempt: int = Field(ge=1, description="Self-repair attempts used, this one included")
    error_count: int = Field(ge=1)
    fields: list[Identifier] = Field(description="Top-level report fields with errors")
    error_types: list[Identifier] = Field(description="Pydantic error types, e.g. 'missing'")


class ReconcileChanges(BaseModel):
    """What reconcile changed in a report: statuses, breaker ids and action indexes only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    degradation_before: DegradationStatus
    degradation_after: DegradationStatus
    breakers_changed: bool
    breakers_after: list[Identifier] = Field(description="Tripped breakers after reconcile")
    approval_raised: list[Annotated[int, Field(ge=0)]] = Field(
        description="Indexes of recommended actions whose requires_approval was raised"
    )


class Reconciled(_TraceEvent):
    """reconcile ran on the report the run is about to return."""

    kind: Literal["reconciled"] = "reconciled"
    changes: ReconcileChanges


class DeadlineExceeded(_TraceEvent):
    """The run deadline expired; a synthesis fallback answers instead."""

    kind: Literal["deadline_exceeded"] = "deadline_exceeded"
    deadline_s: float = Field(gt=0)


class CacheStore(_TraceEvent):
    """The run's report was stored in, or deliberately kept out of, the semantic cache."""

    kind: Literal["cache_store"] = "cache_store"
    backend: CacheBackend
    outcome: Literal["stored", "skipped_fallback", "failed"]
    duration_ms: Milliseconds


class TraceFinished(_TraceEvent):
    """How the request ended; the only event that carries the report."""

    kind: Literal["trace_finished"] = "trace_finished"
    outcome: TriageOutcomeKind
    response: TriageResponse | None = Field(
        description="What POST /triage returns for this request; None for a shed request"
    )

    @model_validator(mode="after")
    def _response_unless_shed(self) -> "TraceFinished":
        if (self.response is None) != (self.outcome == "shed"):
            raise ValueError("response is None exactly when the outcome is shed")
        return self


TraceEvent = Annotated[
    TraceStarted
    | ScenarioSetup
    | CacheLookup
    | RunSlot
    | NodeStarted
    | NodeFinished
    | RouteDecided
    | ModelCall
    | TelemetryRead
    | ValidationFailed
    | Reconciled
    | DeadlineExceeded
    | CacheStore
    | TraceFinished,
    Field(discriminator="kind"),
]

_EVENT_ADAPTER: TypeAdapter[TraceEvent] = TypeAdapter(TraceEvent)


def parse_trace_event(data: str | bytes | dict[str, Any]) -> TraceEvent:
    """Validate one trace event from JSON or a dict; raises ValidationError on an unknown kind."""
    if isinstance(data, dict):
        return _EVENT_ADAPTER.validate_python(data)
    return _EVENT_ADAPTER.validate_json(data)
