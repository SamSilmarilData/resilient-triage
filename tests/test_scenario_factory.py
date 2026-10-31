"""Scenario factory: each scenario run gets fully private collaborators (ADR-009)."""

from dataclasses import replace
from typing import Any

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.config import settings
from resilient_triage.scenarios import Scenario, ScenarioFactory, Setup
from resilient_triage.schemas.telemetry import StatuspageSummary
from resilient_triage.trace.events import (
    ScenarioSetup,
    TelemetryRead,
    TraceEvent,
    TraceStarted,
)
from resilient_triage.trace.recorder import ListTrace
from resilient_triage.triage.gateway import Analyzed
from resilient_triage.triage.slots import RunSlotPool

GITHUB_DEGRADED = StatuspageSummary(
    page_name="GitHub", indicator="major", description="Actions degraded"
)


def _scenario(**changes: Any) -> Scenario:
    """A plain scenario (canned Statuspage, default replies), with `changes` applied."""
    base = Scenario(
        id="test-scenario",
        query="GitHub Actions workflows failing with 503 errors",
        scope=None,
        script=(),
        statuspage=GITHUB_DEGRADED,
        caption="A plain scenario run.",
        expected_outcome="analyzed",
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
