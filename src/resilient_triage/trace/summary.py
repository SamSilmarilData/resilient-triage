"""Trace summary: the condensed, structured execution trace returned with a triage answer.

`summarize()` derives it from the event list, so it is never tracked separately. It works on a
trace without `trace_finished` too, because the summary goes inside the response that event
carries. Structured only: prose renderings belong to the frontend (ADR-008).
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, overload

from pydantic import BaseModel, ConfigDict, Field

from resilient_triage.schemas.incident import DegradationStatus
from resilient_triage.trace.fields import (
    Identifier,
    Milliseconds,
    ModelOutcome,
    ModelRole,
    NodeStatus,
    ReadOutcome,
    ReconcileChanges,
    TriageOutcomeKind,
)

if TYPE_CHECKING:  # trace.events imports schemas.api, which will import this module
    from resilient_triage.trace.events import TraceEvent

_SELF_REPAIR_NODE = "self_repair"
_REFINE_NODE = "refine_reflection"


class _Summary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeVisit(_Summary):
    """One visit to a graph node; duration is None if the node never finished."""

    node: Identifier
    duration_ms: Milliseconds | None = None
    status: NodeStatus = "ok"


class ModelCallSummary(_Summary):
    """One adapter attempt."""

    model: Identifier
    role: ModelRole
    outcome: ModelOutcome
    duration_ms: Milliseconds


class SourceRead(_Summary):
    """One run-time read of one Telemetry source (scenario setup reads are left out)."""

    source: Identifier
    outcome: ReadOutcome
    attempts: int = Field(ge=0)


class SummaryTimings(_Summary):
    """Milliseconds measured from trace_started; None where the step never happened."""

    total_ms: Milliseconds = Field(description="trace_started to the last event")
    cache_lookup_ms: Milliseconds | None = None
    slot_wait_ms: Milliseconds | None = None
    run_ms: Milliseconds | None = Field(
        default=None, description="First node start to last node finish"
    )
    model_ms: Milliseconds = Field(default=0, description="Sum of model call durations")


class TraceSummary(_Summary):
    """The condensed execution trace: what ran, which way, which models, how it ended."""

    scenario_id: Identifier | None = None
    outcome: TriageOutcomeKind | None = Field(
        default=None, description="None if the trace shows neither a cache hit nor a run slot"
    )
    nodes: list[NodeVisit] = Field(default_factory=list, description="Node path, in order")
    routes: list[Identifier] = Field(default_factory=list, description="Route keys taken")
    model_calls: list[ModelCallSummary] = Field(default_factory=list)
    failovers: int = Field(default=0, ge=0, description="Calls made to the fallback model")
    telemetry: list[SourceRead] = Field(default_factory=list)
    repair_rounds: int = Field(default=0, ge=0)
    refine_rounds: int = Field(default=0, ge=0)
    reconciliation: ReconcileChanges | None = None
    degradation: DegradationStatus | None = Field(
        default=None, description="The answer's degradation status, where the trace shows it"
    )
    deadline_exceeded: bool = False
    timings: SummaryTimings = Field(default_factory=lambda: SummaryTimings(total_ms=0))


def summarize(events: Sequence["TraceEvent"]) -> TraceSummary:
    """Condense an event list into its trace summary; pure."""
    origin_ms = 0.0
    scenario_id: str | None = None
    outcome: TriageOutcomeKind | None = None
    finished_outcome: TriageOutcomeKind | None = None
    nodes: list[NodeVisit] = []
    routes: list[str] = []
    model_calls: list[ModelCallSummary] = []
    telemetry: list[SourceRead] = []
    reconciliation: ReconcileChanges | None = None
    degradation: DegradationStatus | None = None
    deadline_exceeded = False
    cache_lookup_ms: float | None = None
    slot_wait_ms: float | None = None
    run_started_ms: float | None = None
    run_finished_ms: float | None = None

    for event in events:
        if event.kind == "trace_started":
            origin_ms, scenario_id = event.t_ms, event.scenario_id
        elif event.kind == "cache_lookup":
            cache_lookup_ms = event.duration_ms
            if event.hit:
                outcome = "cache_hit"
        elif event.kind == "run_slot":
            slot_wait_ms = event.waited_ms
            outcome = "analyzed" if event.acquired else "shed"
        elif event.kind == "node_started":
            nodes.append(NodeVisit(node=event.node))
            if run_started_ms is None:
                run_started_ms = event.t_ms
        elif event.kind == "node_finished":
            nodes = _finish_visit(nodes, event.node, event.duration_ms, event.status)
            run_finished_ms = event.t_ms
        elif event.kind == "route_decided":
            routes.append(event.route)
        elif event.kind == "model_call":
            model_calls.append(
                ModelCallSummary(
                    model=event.model,
                    role=event.role,
                    outcome=event.outcome,
                    duration_ms=event.duration_ms,
                )
            )
        elif event.kind == "telemetry_read" and event.phase is None:
            telemetry.append(
                SourceRead(source=event.source, outcome=event.outcome, attempts=event.attempts)
            )
        elif event.kind == "reconciled":
            reconciliation = event.changes
            degradation = event.changes.degradation_after
        elif event.kind == "deadline_exceeded":
            deadline_exceeded = True
        elif event.kind == "trace_finished":
            finished_outcome = event.outcome
            if event.response is not None:
                degradation = event.response.report.degradation_status

    last_ms = events[-1].t_ms if events else origin_ms
    run_ms = (
        run_finished_ms - run_started_ms
        if run_started_ms is not None and run_finished_ms is not None
        else None
    )
    return TraceSummary(
        scenario_id=scenario_id,
        outcome=finished_outcome or outcome,
        nodes=nodes,
        routes=routes,
        model_calls=model_calls,
        failovers=sum(1 for call in model_calls if call.role == "fallback"),
        telemetry=telemetry,
        repair_rounds=sum(1 for visit in nodes if visit.node == _SELF_REPAIR_NODE),
        refine_rounds=sum(1 for visit in nodes if visit.node == _REFINE_NODE),
        reconciliation=reconciliation,
        degradation=degradation,
        deadline_exceeded=deadline_exceeded,
        timings=SummaryTimings(
            total_ms=_round_ms(max(last_ms - origin_ms, 0.0)),
            cache_lookup_ms=_round_ms(cache_lookup_ms),
            slot_wait_ms=_round_ms(slot_wait_ms),
            run_ms=_round_ms(run_ms),
            model_ms=_round_ms(sum(call.duration_ms for call in model_calls)),
        ),
    )


@overload
def _round_ms(ms: float) -> float: ...
@overload
def _round_ms(ms: None) -> None: ...
def _round_ms(ms: float | None) -> float | None:
    """Round to microseconds, as the recorder stamps t_ms, dropping float noise."""
    return None if ms is None else round(ms, 3)


def _finish_visit(
    nodes: list[NodeVisit], node: str, duration_ms: float, status: NodeStatus
) -> list[NodeVisit]:
    """Close the latest open visit to node (runs are serial, so it is the last one)."""
    for index in range(len(nodes) - 1, -1, -1):
        visit = nodes[index]
        if visit.node == node and visit.duration_ms is None:
            closed = visit.model_copy(update={"duration_ms": duration_ms, "status": status})
            return [*nodes[:index], closed, *nodes[index + 1 :]]
    return nodes
