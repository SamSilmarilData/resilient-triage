"""Scenario factory: each scenario run gets fully private collaborators (ADR-009)."""

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.config import settings
from resilient_triage.llm import DEFAULT_REPORT_JSON, ScriptedReply
from resilient_triage.runtime import AppRuntime
from resilient_triage.scenarios import (
    CacheSeed,
    ExpectedOutcome,
    HeldSlots,
    Overrides,
    Scenario,
    ScenarioFactory,
    Setup,
)
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.telemetry import ChaosConfig, StatuspageSummary
from resilient_triage.trace.events import (
    DeadlineExceeded,
    ModelCall,
    RunSlot,
    ScenarioSetup,
    TelemetryRead,
    TraceEvent,
    TraceStarted,
)
from resilient_triage.trace.recorder import ListTrace
from resilient_triage.triage.gateway import Analyzed, CacheHit, Shed
from resilient_triage.triage.slots import RunSlotPool

SCENARIO_QUERY = "GitHub Actions workflows failing with 503 errors"
SEED_REPORT = IncidentTriageReport.model_validate_json(DEFAULT_REPORT_JSON)
GITHUB_DEGRADED = StatuspageSummary(
    page_name="GitHub", indicator="major", description="Actions degraded"
)


def _scenario(**changes: Any) -> Scenario:
    """A plain scenario (canned Statuspage, default replies), with `changes` applied."""
    base = Scenario(
        id="test-scenario",
        chapter="test-chapter",
        title="Test scenario",
        query=SCENARIO_QUERY,
        scope=None,
        script=(),
        statuspage=GITHUB_DEGRADED,
        caption="A plain scenario run.",
        expected_outcome=ExpectedOutcome(outcome="analyzed"),
    )
    return replace(base, **changes)


def _factory(slots: RunSlotPool | None = None) -> ScenarioFactory:
    """A factory on the test settings, a shared scenario slot pool and a hashing embedder."""
    return ScenarioFactory(
        settings=settings,
        slots=slots or RunSlotPool(capacity=4, queue_timeout_s=settings.triage_queue_timeout_s),
        embedder=DeterministicEmbeddingProvider(),
    )


def _kinds(events: tuple[TraceEvent, ...]) -> list[str]:
    return [event.kind for event in events]


async def test_a_scenario_run_is_analyzed_with_its_scenario_id_on_trace_started() -> None:
    trace = ListTrace()

    outcome = await _factory().build(_scenario()).triage(trace=trace)

    assert isinstance(outcome, Analyzed)
    started = trace.events[0]
    assert isinstance(started, TraceStarted)
    assert started.scenario_id == "test-scenario"
    reads = [e for e in trace.events if isinstance(e, TelemetryRead)]
    assert [(r.source, r.outcome) for r in reads] == [("statuspage", "read")]
    assert _kinds(trace.events)[-1] == "trace_finished"


async def test_pre_run_failures_open_the_private_breaker_at_exactly_fail_max() -> None:
    trace = ListTrace()
    scenario = _scenario(statuspage=None, setup=Setup(tripped_sources=("statuspage",)))

    await _factory().build(scenario).triage(trace=trace)

    fail_max = settings.circuit_breaker_fail_max
    start = _kinds(trace.events).index("trace_started")
    setup, run = trace.events[:start], trace.events[start:]
    setup_reads = [e for e in setup if isinstance(e, TelemetryRead)]
    assert len(setup_reads) == fail_max
    assert all(r.phase == "setup" and r.source == "statuspage" for r in setup_reads)
    assert [r.breaker_after for r in setup_reads] == ["closed"] * (fail_max - 1) + ["open"]
    assert all(r.attempts == settings.retry_max_attempts for r in setup_reads)
    assert setup[-1] == ScenarioSetup(
        seq=fail_max, t_ms=setup[-1].t_ms, step="fail_source", count=fail_max, source="statuspage"
    )
    run_reads = [e for e in run if isinstance(e, TelemetryRead)]
    assert [(r.phase, r.outcome, r.attempts) for r in run_reads] == [(None, "tripped", 0)]


async def test_setup_runs_before_the_clock_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real-sized retry waits make the tripping setup take a noticeable time; none of it may
    # count towards the run's clock, which starts at trace_started (#112).
    monkeypatch.setattr(settings, "retry_min_wait_seconds", 0.02)
    monkeypatch.setattr(settings, "retry_max_wait_seconds", 0.02)
    trace = ListTrace()
    scenario = _scenario(statuspage=None, setup=Setup(tripped_sources=("statuspage",)))

    await _factory().build(scenario).triage(trace=trace)

    start = _kinds(trace.events).index("trace_started")
    setup_ms = sum(e.duration_ms for e in trace.events[:start] if isinstance(e, TelemetryRead))
    assert setup_ms >= 100  # the setup really waited
    assert all(e.t_ms == 0 for e in trace.events[: start + 1])
    assert trace.events[-1].t_ms < setup_ms


async def test_a_seeded_scenario_is_answered_from_its_private_cache() -> None:
    trace = ListTrace()
    seed = CacheSeed(query=SCENARIO_QUERY, report=SEED_REPORT)
    run = _factory().build(
        _scenario(setup=Setup(seeds=(seed,)), expected_outcome=ExpectedOutcome(outcome="cache_hit"))
    )

    outcome = await run.triage(trace=trace)

    assert isinstance(outcome, CacheHit)
    assert outcome.report == SEED_REPORT
    assert [(e.kind, getattr(e, "step", None)) for e in trace.events] == [
        ("scenario_setup", "seed_cache"),
        ("trace_started", None),
        ("cache_lookup", None),
        ("trace_finished", None),
    ]
    assert isinstance(trace.events[0], ScenarioSetup) and trace.events[0].count == 1


async def test_held_slots_shed_the_run_on_a_private_pool_and_are_released() -> None:
    trace = ListTrace()
    shared = RunSlotPool(capacity=4, queue_timeout_s=settings.triage_queue_timeout_s)
    held = HeldSlots(capacity=2, queue_timeout_s=0.02)
    run = _factory(shared).build(
        _scenario(
            overrides=Overrides(held_slots=held), expected_outcome=ExpectedOutcome(outcome="shed")
        )
    )

    outcome = await run.triage(trace=trace)

    assert isinstance(outcome, Shed)
    assert _kinds(trace.events) == [
        "scenario_setup",
        "trace_started",
        "cache_lookup",
        "run_slot",
        "trace_finished",
    ]
    hold, slot = trace.events[0], trace.events[3]
    assert isinstance(hold, ScenarioSetup)
    assert (hold.step, hold.count) == ("hold_run_slots", 2)
    assert isinstance(slot, RunSlot)
    assert (slot.acquired, slot.capacity) == (False, 2)
    assert run.slots is not shared
    assert run.slots.free == 2  # the held slots were given back after the run
    assert shared.free == 4


@pytest.mark.real_scripted_delays
async def test_a_declared_reply_delay_shows_up_in_the_trace_timings() -> None:
    trace = ListTrace()
    script = (ScriptedReply(DEFAULT_REPORT_JSON, delay_s=0.2),)

    await _factory().build(_scenario(script=script)).triage(trace=trace)

    started = trace.events[0]
    call = next(e for e in trace.events if isinstance(e, ModelCall))
    assert call.duration_ms >= 200
    assert call.t_ms - started.t_ms >= 200


async def test_declared_reply_delays_are_zeroed_in_tests_by_default() -> None:
    script = (ScriptedReply(DEFAULT_REPORT_JSON, delay_s=60),)

    async with asyncio.timeout(5):
        outcome = await _factory().build(_scenario(script=script)).triage()

    assert isinstance(outcome, Analyzed)


@pytest.mark.real_scripted_delays
async def test_a_run_deadline_override_cuts_off_only_that_run() -> None:
    trace = ListTrace()
    slow = (ScriptedReply(DEFAULT_REPORT_JSON, delay_s=5),)
    scenario = _scenario(script=slow, overrides=Overrides(run_deadline_s=0.1))

    async with asyncio.timeout(3):
        outcome = await _factory().build(scenario).triage(trace=trace)

    assert isinstance(outcome, Analyzed)
    deadlines = [e for e in trace.events if isinstance(e, DeadlineExceeded)]
    assert [d.deadline_s for d in deadlines] == [0.1]
    assert settings.triage_run_deadline_s != 0.1  # the shared setting is untouched


async def test_a_run_deadline_override_is_shown_as_scenario_setup() -> None:
    trace = ListTrace()

    await _factory().build(_scenario(overrides=Overrides(run_deadline_s=3))).triage(trace=trace)

    assert _kinds(trace.events)[:2] == ["scenario_setup", "trace_started"]
    assert trace.events[0] == ScenarioSetup(step="set_run_deadline", count=1, deadline_s=3)


async def test_concurrent_runs_share_nothing_and_leave_the_shared_runtime_alone(
    runtime: AppRuntime,
) -> None:
    before = await runtime.snapshot()
    metrics_before = runtime.triage_gateway.metrics
    factory = ScenarioFactory(
        settings=settings,
        slots=RunSlotPool(capacity=4, queue_timeout_s=settings.triage_queue_timeout_s),
        embedder=runtime.cache.embedding_provider,
    )
    tripped = _scenario(
        id="tripped",
        statuspage=None,
        setup=Setup(
            tripped_sources=("statuspage",),
            seeds=(CacheSeed(query="Stripe webhooks timing out", report=SEED_REPORT),),
        ),
    )
    a, b = factory.build(tripped), factory.build(_scenario(id="plain"))
    trace_a, trace_b = ListTrace(), ListTrace()

    await asyncio.gather(a.triage(trace=trace_a), b.triage(trace=trace_b))

    for private in ("gateway", "breakers", "cache", "llm", "telemetry", "chaos"):
        assert getattr(a, private) is not getattr(b, private), private
        assert getattr(a, private) is not getattr(runtime, private, None), private
    assert a.deps.telemetry is not runtime.deps.telemetry
    assert (a.cache.count, b.cache.count) == (2, 1)  # A's seed and answer; B's answer
    assert a.breakers.get_all_statuses()["statuspage"].is_open
    assert not b.breakers.get_all_statuses()["statuspage"].is_open
    run_reads_b = [e for e in trace_b.events if isinstance(e, TelemetryRead)]
    assert [(r.outcome, r.breaker_after) for r in run_reads_b] == [("read", "closed")]
    after = await runtime.snapshot()
    assert after == before
    assert runtime.triage_gateway.metrics == metrics_before


SENTINEL_DOWN = ChaosConfig(active=True, inject_503=True)
INTERNAL_QUERY = "Internal auth service failing with 503 errors"


async def test_a_cluster_sentinel_fault_is_set_on_the_private_chaos_manager_only(
    runtime: AppRuntime,
) -> None:
    trace = ListTrace()
    scenario = _scenario(query=INTERNAL_QUERY, cluster_sentinel=SENTINEL_DOWN)

    await _factory().build(scenario).triage(trace=trace)

    reads = [e for e in trace.events if isinstance(e, TelemetryRead)]
    assert [(r.source, r.outcome) for r in reads] == [("chaos", "unavailable")]
    assert runtime.chaos.get_config() == ChaosConfig()


async def test_a_failing_cluster_sentinel_can_be_tripped_in_setup() -> None:
    trace = ListTrace()
    scenario = _scenario(
        query=INTERNAL_QUERY,
        cluster_sentinel=SENTINEL_DOWN,
        setup=Setup(tripped_sources=("chaos",)),
    )

    await _factory().build(scenario).triage(trace=trace)

    setup_reads = [e for e in trace.events if isinstance(e, TelemetryRead) and e.phase]
    assert len(setup_reads) == settings.circuit_breaker_fail_max
    assert setup_reads[-1].breaker_after == "open"


def test_a_scenario_cannot_trip_a_source_that_does_not_fail() -> None:
    with pytest.raises(ValueError, match="statuspage"):
        _scenario(setup=Setup(tripped_sources=("statuspage",)))
    with pytest.raises(ValueError, match="chaos"):
        _scenario(setup=Setup(tripped_sources=("chaos",)))


def test_a_scenario_fault_must_repeat_exactly() -> None:
    with pytest.raises(ValueError, match="failure_rate"):
        _scenario(cluster_sentinel=ChaosConfig(active=True, failure_rate=0.5))


async def test_a_streamed_run_carries_its_setup_before_trace_started() -> None:
    held = HeldSlots(capacity=1, queue_timeout_s=0.02)
    seed = CacheSeed(query="Stripe webhooks timing out", report=SEED_REPORT)
    scenario = _scenario(setup=Setup(seeds=(seed,)), overrides=Overrides(held_slots=held))
    run = _factory().build(scenario)

    async with run.stream() as events:
        streamed = [event async for event in events]

    assert _kinds(tuple(streamed)) == [
        "scenario_setup",
        "scenario_setup",
        "trace_started",
        "cache_lookup",
        "run_slot",
        "trace_finished",
    ]
    assert [e.seq for e in streamed] == list(range(6))
    started = streamed[2]
    assert isinstance(started, TraceStarted) and started.scenario_id == "test-scenario"
    assert run.slots.free == 1


async def _setup_read_fails(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("Secret setup failure")


async def test_a_failing_setup_is_answered_with_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = ListTrace()
    held = HeldSlots(capacity=1, queue_timeout_s=0.02)
    scenario = _scenario(
        statuspage=None,
        setup=Setup(tripped_sources=("statuspage",)),
        overrides=Overrides(held_slots=held),
    )
    run = _factory().build(scenario)
    monkeypatch.setattr(run.telemetry, "collect", _setup_read_fails)

    outcome = await run.triage(trace=trace)

    assert isinstance(outcome, Analyzed)
    assert outcome.report.degradation_status == "SEVERELY_DEGRADED"
    assert outcome.response.cached is False
    assert _kinds(trace.events) == ["trace_started", "reconciled", "trace_finished"]
    assert run.cache.count == 0  # the fallback is not cached
    assert run.slots.free == 1


async def test_a_failing_setup_still_streams_to_trace_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _factory().build(_scenario(statuspage=None, setup=Setup(tripped_sources=("statuspage",))))
    monkeypatch.setattr(run.telemetry, "collect", _setup_read_fails)

    async with run.stream() as events:
        streamed = [event async for event in events]

    assert _kinds(tuple(streamed)) == ["trace_started", "reconciled", "trace_finished"]
    started = streamed[0]
    assert isinstance(started, TraceStarted) and started.scenario_id == "test-scenario"
