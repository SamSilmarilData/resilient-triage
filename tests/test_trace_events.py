"""Tests for trace events, the trace recorders and the trace summary (ADR-008, #108)."""

import json
from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from resilient_triage.graph.reconciliation import DegradationEvidence, reconcile
from resilient_triage.llm.failures import (
    MalformedResponse,
    ProviderFailure,
    RateLimited,
    Rejected,
    Unavailable,
)
from resilient_triage.llm.gateway import Completion
from resilient_triage.schemas.api import TriageResponse
from resilient_triage.schemas.incident import (
    ActionRecommendation,
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
from resilient_triage.trace.summary import (
    ModelCallSummary,
    NodeVisit,
    SourceRead,
    SummaryTimings,
    TraceSummary,
    summarize,
)


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


# Free text a caller could leak: query, prompt, model output, snippet or Pydantic input.
SENTINEL = "Secret customer query: card 4111 failing"


def _string_schemas(node: object, defs: dict[str, object], path: str) -> list[tuple[str, dict]]:
    """Every string-typed schema reachable from node, except inside TriageResponse."""
    if isinstance(node, list):
        return [s for item in node for s in _string_schemas(item, defs, path)]
    if not isinstance(node, dict):
        return []
    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        if name == "TriageResponse":  # the report travels only in trace_finished
            return []
        return _string_schemas(defs[name], defs, f"{path}->{name}")
    found = [(path, node)] if node.get("type") == "string" else []
    for key, child in node.items():
        if key != "$defs":
            found += _string_schemas(child, defs, f"{path}.{key}")
    return found


def test_no_event_field_can_hold_free_text() -> None:
    schema = TypeAdapter(TraceEvent).json_schema()
    strings = _string_schemas(schema, schema["$defs"], "TraceEvent")

    assert strings, "the walk found no string fields at all"
    free_text = [p for p, s in strings if not ({"enum", "const", "pattern"} & s.keys())]
    assert free_text == []


@pytest.mark.parametrize("event", one_of_each_kind(), ids=lambda e: e.kind)
def test_sentinel_is_rejected_by_every_string_field(event: TraceEvent) -> None:
    data = event.model_dump(mode="json")
    for name, value in data.items():
        if name == "kind" or not isinstance(value, str):
            continue
        with pytest.raises(ValidationError):
            type(event).model_validate({**data, name: SENTINEL})


def _invalid_report_error() -> ValidationError:
    try:
        IncidentTriageReport.model_validate(
            {
                "summary": SENTINEL,
                "severity": SENTINEL,
                "root_cause_analysis": SENTINEL,
                "confidence_score": SENTINEL,
                SENTINEL: SENTINEL,
            }
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("expected the report to be invalid")


def _events_built_from_sentinel_inputs() -> list[TraceEvent]:
    before = _report(SENTINEL).model_copy(
        update={
            "circuit_breakers_tripped": [SENTINEL],
            "recommended_actions": [
                ActionRecommendation(
                    action=SENTINEL, target_system=SENTINEL, action_type="remediative"
                ),
                ActionRecommendation(
                    action=SENTINEL, target_system=SENTINEL, action_type="diagnostic"
                ),
            ],
        }
    )
    evidence = DegradationEvidence(
        tripped_breakers=frozenset({"statuspage"}),
        unavailable_sources=frozenset(),
        synthesis_fell_back=False,
    )
    after = reconcile(before, evidence)
    results: list[Completion | ProviderFailure] = [
        Completion(text=SENTINEL, model="qwen/qwen3.8-27b"),
        RateLimited(retry_after_s=30.0, scope="minute", detail=SENTINEL),
        Unavailable(detail=SENTINEL),
        Rejected(detail=SENTINEL),
        MalformedResponse(detail=SENTINEL),
    ]
    return [
        *(
            ModelCall.from_result(
                model="qwen/qwen3.8-27b", role="primary", result=result, duration_ms=5.0
            )
            for result in results
        ),
        ValidationFailed.from_error(_invalid_report_error(), attempt=1),
        ValidationFailed.from_error(json.JSONDecodeError(SENTINEL, SENTINEL, 0), attempt=2),
        Reconciled.between(before, after),
        TraceFinished(
            outcome="analyzed", response=TriageResponse(report=after, execution_time_ms=9.0)
        ),
    ]


def test_sentinel_reaches_the_wire_only_inside_trace_finished() -> None:
    trace = ListTrace()
    for event in [*one_of_each_kind(), *_events_built_from_sentinel_inputs()]:
        trace.record(event)

    leaks = [e.kind for e in trace.events if SENTINEL in e.model_dump_json()]
    assert leaks == ["trace_finished"]


def test_model_call_keeps_the_failure_kind_not_its_detail() -> None:
    limited = ModelCall.from_result(
        model="llama-3.1-8b-instant",
        role="fallback",
        result=RateLimited(retry_after_s=12.0, scope="day", detail=SENTINEL),
        duration_ms=3.0,
    )
    assert (limited.outcome, limited.rate_limit_scope, limited.retry_after_s) == (
        "rate_limited",
        "day",
        12.0,
    )

    malformed = ModelCall.from_result(
        model="qwen/qwen3.8-27b", role="primary", result=MalformedResponse("x"), duration_ms=1
    )
    assert (malformed.outcome, malformed.rate_limit_scope) == ("malformed_response", None)


def test_validation_failed_names_fields_and_error_types_only() -> None:
    event = ValidationFailed.from_error(_invalid_report_error(), attempt=1)

    assert event.fields == ["confidence_score", "severity"]
    assert event.error_types == ["enum", "extra_forbidden", "float_parsing"]
    assert event.error_count == 3


def test_unparseable_json_is_one_json_invalid_error() -> None:
    event = ValidationFailed.from_error(json.JSONDecodeError("bad", "{", 0), attempt=2)

    assert (event.attempt, event.error_count, event.fields, event.error_types) == (
        2,
        1,
        [],
        ["json_invalid"],
    )


def test_reconciled_records_status_breakers_and_raised_approvals() -> None:
    before = _report().model_copy(
        update={
            "circuit_breakers_tripped": ["made-up-breaker"],
            "recommended_actions": [
                ActionRecommendation(
                    action="inspect", target_system="api", action_type="diagnostic"
                ),
                ActionRecommendation(
                    action="restart", target_system="api", action_type="remediative"
                ),
            ],
        }
    )
    evidence = DegradationEvidence(
        tripped_breakers=frozenset({"statuspage"}),
        unavailable_sources=frozenset(),
        synthesis_fell_back=False,
    )

    changes = Reconciled.between(before, reconcile(before, evidence)).changes

    assert changes == ReconcileChanges(
        degradation_before=DegradationStatus.HEALTHY,
        degradation_after=DegradationStatus.PARTIALLY_DEGRADED,
        breakers_changed=True,
        breakers_after=["statuspage"],
        approval_raised=[1],
    )


def _numbered(events: list[TraceEvent]) -> list[TraceEvent]:
    """Stamp seq in order (t_ms is set per event by the test)."""
    return [e.model_copy(update={"seq": i}) for i, e in enumerate(events)]


REFINE_RUN_CHANGES = ReconcileChanges(
    degradation_before=DegradationStatus.HEALTHY,
    degradation_after=DegradationStatus.PARTIALLY_DEGRADED,
    breakers_changed=False,
    breakers_after=[],
    approval_raised=[0],
)


def _refine_run_events() -> list[TraceEvent]:
    """A scenario run: setup, cache miss, a degraded read, failover, refine, reconcile."""
    return _numbered(
        [
            TelemetryRead(
                t_ms=1,
                source="statuspage",
                phase="setup",
                outcome="unavailable",
                attempts=3,
                breaker_before="closed",
                breaker_after="closed",
                signals=1,
                duration_ms=1,
            ),
            TraceStarted(t_ms=10, scenario_id="refine"),
            CacheLookup(t_ms=12, backend="in_memory", hit=False, threshold=0.9, duration_ms=2),
            RunSlot(t_ms=13, acquired=True, waited_ms=0.5, capacity=4),
            NodeStarted(t_ms=14, node="collect_telemetry"),
            TelemetryRead(
                t_ms=40,
                source="statuspage",
                outcome="unavailable",
                attempts=3,
                breaker_before="closed",
                breaker_after="closed",
                signals=1,
                duration_ms=25,
            ),
            NodeFinished(t_ms=41, node="collect_telemetry", duration_ms=27),
            NodeStarted(t_ms=41, node="generate_report"),
            ModelCall(
                t_ms=60,
                model="qwen/qwen3.8-27b",
                role="primary",
                outcome="rate_limited",
                rate_limit_scope="minute",
                retry_after_s=30,
                duration_ms=18,
            ),
            ModelCall(
                t_ms=100,
                model="llama-3.1-8b-instant",
                role="fallback",
                outcome="completion",
                duration_ms=40,
            ),
            NodeFinished(t_ms=100, node="generate_report", duration_ms=59),
            NodeStarted(t_ms=100, node="validate_report"),
            NodeFinished(t_ms=101, node="validate_report", duration_ms=1, confidence=0.45),
            RouteDecided(
                t_ms=101, source="validate_report", target="refine_reflection", route="refine"
            ),
            NodeStarted(t_ms=101, node="refine_reflection"),
            NodeFinished(t_ms=102, node="refine_reflection", duration_ms=1),
            NodeStarted(t_ms=102, node="finalize_report"),
            Reconciled(t_ms=103, changes=REFINE_RUN_CHANGES),
            NodeFinished(t_ms=103, node="finalize_report", duration_ms=1),
            CacheStore(t_ms=105, backend="in_memory", outcome="stored", duration_ms=2),
            TraceFinished(
                t_ms=106,
                outcome="analyzed",
                response=TriageResponse(
                    report=_report().model_copy(
                        update={"degradation_status": DegradationStatus.PARTIALLY_DEGRADED}
                    ),
                    execution_time_ms=96.0,
                ),
            ),
        ]
    )


def test_summary_of_an_analyzed_run() -> None:
    summary = summarize(_refine_run_events())

    assert summary == TraceSummary(
        scenario_id="refine",
        outcome="analyzed",
        nodes=[
            NodeVisit(node="collect_telemetry", duration_ms=27),
            NodeVisit(node="generate_report", duration_ms=59),
            NodeVisit(node="validate_report", duration_ms=1),
            NodeVisit(node="refine_reflection", duration_ms=1),
            NodeVisit(node="finalize_report", duration_ms=1),
        ],
        routes=["refine"],
        model_calls=[
            ModelCallSummary(
                model="qwen/qwen3.8-27b", role="primary", outcome="rate_limited", duration_ms=18
            ),
            ModelCallSummary(
                model="llama-3.1-8b-instant", role="fallback", outcome="completion", duration_ms=40
            ),
        ],
        failovers=1,
        telemetry=[SourceRead(source="statuspage", outcome="unavailable", attempts=3)],
        repair_rounds=0,
        refine_rounds=1,
        reconciliation=REFINE_RUN_CHANGES,
        degradation=DegradationStatus.PARTIALLY_DEGRADED,
        deadline_exceeded=False,
        timings=SummaryTimings(
            total_ms=96, cache_lookup_ms=2, slot_wait_ms=0.5, run_ms=89, model_ms=58
        ),
    )


def test_summary_is_the_same_before_trace_finished_except_for_the_finish() -> None:
    events = _refine_run_events()

    before_finish = summarize(events[:-1])

    assert before_finish.outcome == "analyzed"
    assert before_finish.degradation == DegradationStatus.PARTIALLY_DEGRADED
    assert before_finish.timings.total_ms == 95
    assert before_finish.model_dump(exclude={"timings"}) == summarize(events).model_dump(
        exclude={"timings"}
    )


def test_summary_of_a_cache_hit() -> None:
    events = _numbered(
        [
            TraceStarted(t_ms=0.5),
            CacheLookup(
                t_ms=3,
                backend="redisearch",
                hit=True,
                similarity=0.97,
                threshold=0.9,
                duration_ms=2.5,
            ),
            TraceFinished(
                t_ms=3.5,
                outcome="cache_hit",
                response=TriageResponse(
                    report=_report(), cached=True, cache_similarity=0.97, execution_time_ms=3
                ),
            ),
        ]
    )

    summary = summarize(events)

    assert (summary.outcome, summary.nodes, summary.degradation) == (
        "cache_hit",
        [],
        DegradationStatus.HEALTHY,
    )
    assert summary.timings == SummaryTimings(
        total_ms=3, cache_lookup_ms=2.5, slot_wait_ms=None, run_ms=None, model_ms=0
    )


def test_summary_of_a_shed_request_without_trace_finished() -> None:
    events = _numbered(
        [
            TraceStarted(t_ms=0),
            CacheLookup(t_ms=1, backend="in_memory", hit=False, threshold=0.9, duration_ms=1),
            RunSlot(t_ms=1501, acquired=False, waited_ms=1500, capacity=2),
        ]
    )

    summary = summarize(events)

    assert (summary.outcome, summary.timings.slot_wait_ms, summary.degradation) == (
        "shed",
        1500,
        None,
    )


def test_summary_of_a_deadline_counts_repairs_and_unfinished_nodes() -> None:
    events = _numbered(
        [
            TraceStarted(t_ms=0, force_refresh=True),
            RunSlot(t_ms=0, acquired=True, waited_ms=0, capacity=4),
            NodeStarted(t_ms=1, node="validate_report"),
            ValidationFailed(
                t_ms=2, attempt=1, error_count=1, fields=[], error_types=["json_invalid"]
            ),
            NodeFinished(t_ms=2, node="validate_report", duration_ms=1),
            RouteDecided(t_ms=2, source="validate_report", target="self_repair", route="repair"),
            NodeStarted(t_ms=2, node="self_repair"),
            NodeFinished(t_ms=3, node="self_repair", duration_ms=1),
            NodeStarted(t_ms=3, node="generate_report"),
            NodeFinished(t_ms=3000, node="generate_report", duration_ms=2997, status="cancelled"),
            DeadlineExceeded(t_ms=3000, deadline_s=3),
        ]
    )

    summary = summarize(events)

    assert summary.repair_rounds == 1
    assert summary.deadline_exceeded is True
    assert summary.nodes[-1] == NodeVisit(
        node="generate_report", duration_ms=2997, status="cancelled"
    )
    assert summary.routes == ["repair"]


def test_summary_of_no_events_is_empty() -> None:
    summary = summarize([])

    assert (summary.outcome, summary.nodes, summary.timings.total_ms) == (None, [], 0)


def test_list_trace_summary_matches_summarize_of_its_events() -> None:
    trace = ListTrace()
    for event in _refine_run_events():
        trace.record(event)

    assert trace.summary() == summarize(trace.events)


def test_no_summary_field_can_hold_free_text() -> None:
    schema = TraceSummary.model_json_schema()
    strings = _string_schemas(schema, schema.get("$defs", {}), "TraceSummary")

    assert strings
    assert [p for p, s in strings if not ({"enum", "const", "pattern"} & s.keys())] == []
