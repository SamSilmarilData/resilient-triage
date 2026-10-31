"""Tests for trace events, the trace recorders and the trace summary (ADR-008, #108)."""

from typing import get_args

import pytest
from pydantic import ValidationError

from resilient_triage.schemas.api import TriageResponse
from resilient_triage.schemas.incident import (
    DegradationStatus,
    IncidentTriageReport,
    SeverityLevel,
)
from resilient_triage.trace.events import (
    TRACE_SCHEMA_VERSION,
    CacheLookup,
    CacheStore,
    DeadlineExceeded,
    ModelCall,
    NodeFinished,
    NodeStarted,
    ReconcileChanges,
    Reconciled,
    RouteDecided,
    RunSlot,
    ScenarioSetup,
    TelemetryRead,
    TraceEvent,
    TraceFinished,
    TraceStarted,
    ValidationFailed,
    parse_trace_event,
)
from resilient_triage.trace.recorder import ListTrace, NullTrace, TraceRecorder


def _report(summary: str = "Payment API elevated errors") -> IncidentTriageReport:
    return IncidentTriageReport(
        summary=summary,
        severity=SeverityLevel.HIGH,
        root_cause_analysis="Connection pool exhaustion",
    )


def one_of_each_kind() -> list[TraceEvent]:
    """A valid example of every trace event kind, in a plausible order."""
    return [
        ScenarioSetup(step="seed_cache", count=1),
        TraceStarted(scenario_id="cache-hit", scoped=False, force_refresh=False),
        CacheLookup(
            backend="in_memory", hit=False, similarity=None, threshold=0.9, duration_ms=1.5
        ),
        RunSlot(acquired=True, waited_ms=0.2, capacity=4),
        NodeStarted(node="collect_telemetry"),
        TelemetryRead(
            source="statuspage",
            phase="setup",
            outcome="tripped",
            attempts=3,
            breaker_before="closed",
            breaker_after="open",
            signals=1,
            duration_ms=210.0,
        ),
        ModelCall(
            model="qwen/qwen3.8-27b",
            role="primary",
            outcome="rate_limited",
            rate_limit_scope="minute",
            retry_after_s=30.0,
            duration_ms=180.0,
        ),
        NodeFinished(node="validate_report", duration_ms=0.4, confidence=0.45),
        RouteDecided(source="validate_report", target="refine_reflection", route="refine"),
        ValidationFailed(
            attempt=1, error_count=2, fields=["severity"], error_types=["enum", "missing"]
        ),
        Reconciled(
            changes=ReconcileChanges(
                degradation_before=DegradationStatus.HEALTHY,
                degradation_after=DegradationStatus.PARTIALLY_DEGRADED,
                breakers_changed=True,
                breakers_after=["statuspage"],
                approval_raised=[0],
            )
        ),
        DeadlineExceeded(deadline_s=3.0),
        CacheStore(backend="in_memory", outcome="stored", duration_ms=2.0),
        TraceFinished(
            outcome="analyzed",
            response=TriageResponse(report=_report(), execution_time_ms=12.0),
        ),
    ]


def _kinds_in_union() -> set[str]:
    members = get_args(get_args(TraceEvent)[0])
    return {member.model_fields["kind"].default for member in members}


def test_examples_cover_every_kind_in_the_union() -> None:
    assert {event.kind for event in one_of_each_kind()} == _kinds_in_union()
    assert _kinds_in_union() == {
        "trace_started",
        "scenario_setup",
        "cache_lookup",
        "run_slot",
        "node_started",
        "node_finished",
        "route_decided",
        "model_call",
        "telemetry_read",
        "validation_failed",
        "reconciled",
        "deadline_exceeded",
        "cache_store",
        "trace_finished",
    }


@pytest.mark.parametrize("event", one_of_each_kind(), ids=lambda e: e.kind)
def test_every_kind_round_trips_through_the_union(event: TraceEvent) -> None:
    parsed = parse_trace_event(event.model_dump_json())

    assert type(parsed) is type(event)
    assert parsed == event


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_trace_event('{"kind": "span_opened", "seq": 0, "t_ms": 0.0}')


def test_trace_started_carries_the_schema_version() -> None:
    started = TraceStarted(scenario_id=None)

    assert started.schema_version == TRACE_SCHEMA_VERSION == 1


def test_events_are_frozen() -> None:
    event = NodeStarted(node="analyze_query")

    with pytest.raises(ValidationError):
        event.node = "finalize_report"  # type: ignore[misc]


def test_shed_trace_finished_has_no_response_and_others_must() -> None:
    TraceFinished(outcome="shed", response=None)
    with pytest.raises(ValidationError):
        TraceFinished(outcome="analyzed", response=None)
    with pytest.raises(ValidationError):
        TraceFinished(
            outcome="shed", response=TriageResponse(report=_report(), execution_time_ms=1.0)
        )


def test_confidence_is_a_bounded_number() -> None:
    with pytest.raises(ValidationError):
        NodeFinished(node="validate_report", duration_ms=1.0, confidence=1.7)


class FakeClock:
    """A monotonic clock that the test advances by hand, in seconds."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_list_trace_stamps_seq_and_ms_since_it_began() -> None:
    clock = FakeClock()
    trace = ListTrace(clock=clock)
    clock.now += 0.0125
    trace.record(NodeStarted(node="analyze_query"))
    clock.now += 0.25
    trace.record(NodeFinished(node="analyze_query", duration_ms=250.0))

    assert [(e.seq, e.t_ms, e.kind) for e in trace.events] == [
        (0, 12.5, "node_started"),
        (1, 262.5, "node_finished"),
    ]


def test_record_swallows_an_exception_from_a_broken_sink(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_sink(event: TraceEvent) -> None:
        raise RuntimeError("sink is gone")

    trace = ListTrace(sink=broken_sink)
    trace.record(NodeStarted(node="analyze_query"))
    trace.record(NodeStarted(node="collect_telemetry"))

    assert [e.seq for e in trace.events] == [0, 1]
    assert "sink is gone" in caplog.text


def test_record_swallows_an_exception_from_a_clock_that_breaks_mid_trace() -> None:
    readings = iter([100.0])

    def clock() -> float:
        return next(readings)  # StopIteration after the first reading

    trace = ListTrace(clock=clock)
    trace.record(NodeStarted(node="analyze_query"))


def test_sink_sees_each_stamped_event_in_order() -> None:
    seen: list[TraceEvent] = []
    trace = ListTrace(sink=seen.append)
    trace.record(NodeStarted(node="analyze_query"))
    trace.record(NodeStarted(node="collect_telemetry"))

    assert seen == list(trace.events)


def test_null_trace_records_nothing_and_never_raises() -> None:
    recorder: TraceRecorder = NullTrace()
    for event in one_of_each_kind():
        recorder.record(event)
