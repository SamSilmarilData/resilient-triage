"""Field types shared by trace events and the trace summary: ids, Literals and timings only.

A leaf module (no `schemas.api` import), so `trace.summary` can use these types while
`schemas.api` imports `trace.summary` and `trace.events` imports `schemas.api`.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from resilient_triage.schemas.incident import DegradationStatus

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
ReadOutcome = Literal["read", "tripped", "unavailable"]
NodeStatus = Literal["ok", "error", "cancelled"]
TriageOutcomeKind = Literal["cache_hit", "analyzed", "shed"]


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
