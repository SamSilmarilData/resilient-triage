"""The request path's execution trace (ADR-008): what TriageGateway.triage(trace=) records.

Sequences are compared on (kind, key fields) only: no timing assertions.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings, settings
from resilient_triage.graph.deps import TriageDeps
from resilient_triage.llm import DEFAULT_REPORT_JSON, RateLimited, ScriptedAdapter
from resilient_triage.resilience.circuit_breaker import force_open_for_tests
from resilient_triage.runtime import AppRuntime
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport, SeverityLevel
from resilient_triage.schemas.state import TriageState
from resilient_triage.schemas.telemetry import ChaosConfig
from resilient_triage.trace.events import Reconciled, TraceEvent, TraceFinished
from resilient_triage.trace.recorder import ListTrace, NullTrace, TraceRecorder
from resilient_triage.trace.summary import summarize
from resilient_triage.triage.gateway import Analyzed, CacheHit, Shed, TriageGateway

UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
QUERY = "Payments API returning 503 for card checkouts"
OTHER_QUERY = "Checkout latency spike in eu-west-1"
GATEWAY_KINDS = {
    "trace_started",
    "cache_lookup",
    "run_slot",
    "deadline_exceeded",
    "reconciled",
    "cache_store",
    "trace_finished",
}


def _key(event: TraceEvent) -> tuple[Any, ...]:
    """An event's kind and the fields a sequence test compares (never timings)."""
    match event.kind:
        case "trace_started":
            return (event.kind, event.scenario_id, event.scoped, event.force_refresh)
        case "cache_lookup":
            return (event.kind, event.hit)
        case "run_slot":
            return (event.kind, event.acquired)
        case "cache_store":
            return (event.kind, event.outcome)
        case "trace_finished":
            return (event.kind, event.outcome, event.response is not None)
        case "reconciled":
            return (event.kind, event.changes.degradation_after)
        case "node_started" | "node_finished":
            return (event.kind, event.node)
        case _:
            return (event.kind,)


def _gateway_keys(trace: ListTrace) -> list[tuple[Any, ...]]:
    return [_key(e) for e in trace.events if e.kind in GATEWAY_KINDS]


def _finished(trace: ListTrace) -> TraceFinished:
    last = trace.events[-1]
    assert isinstance(last, TraceFinished)
    return last


def _report(summary: str = "Payments API returning 503") -> IncidentTriageReport:
    return IncidentTriageReport(
        summary=summary,
        severity=SeverityLevel.HIGH,
        root_cause_analysis="Upstream payment processor outage",
        degradation_status=DegradationStatus.HEALTHY,
        circuit_breakers_tripped=[],
        confidence_score=0.9,
    )


class FakeRunner:
    """A GraphRunner that returns a state, sleeps, blocks or raises; records each trace given."""

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        raises: BaseException | None = None,
        sleep_s: float = 0.0,
    ) -> None:
        self.state = state if state is not None else {"final_report": _report()}
        self.raises = raises
        self.sleep_s = sleep_s
        self.started = asyncio.Event()
        self.traces: list[TraceRecorder] = []

    async def __call__(self, run_input: TriageState, /, *, trace: TraceRecorder) -> dict[str, Any]:
        self.traces.append(trace)
        self.started.set()
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        if self.raises is not None:
            raise self.raises
        return self.state


@pytest_asyncio.fixture
async def cache() -> AsyncIterator[SemanticCacheManager]:
    """An in-memory semantic cache (Redis unreachable) with offline embeddings."""
    manager = SemanticCacheManager(
        redis_url=UNREACHABLE_REDIS, embedding_provider=DeterministicEmbeddingProvider()
    )
    await manager.initialize()
    yield manager
    await manager.close()


def _settings(**update: Any) -> Settings:
    return settings.model_copy(update=update)


async def test_a_cache_hit_records_exactly_three_events(runtime: AppRuntime) -> None:
    await runtime.triage_gateway.triage(QUERY, "billing")
    trace = ListTrace()

    outcome = await runtime.triage_gateway.triage(QUERY, "billing", trace=trace)

    assert isinstance(outcome, CacheHit)
    assert [_key(e) for e in trace.events] == [
        ("trace_started", None, True, False),
        ("cache_lookup", True),
        ("trace_finished", "cache_hit", True),
    ]
    assert _finished(trace).response == outcome.response
    assert outcome.response.cached is True
    assert outcome.response.trace is not None
    assert outcome.response.trace.outcome == "cache_hit"


async def test_an_analyzed_request_records_the_gateway_steps_around_the_graph_run(
    runtime: AppRuntime,
) -> None:
    trace = ListTrace()

    outcome = await runtime.triage_gateway.triage(QUERY, None, trace=trace)

    assert isinstance(outcome, Analyzed)
    assert _gateway_keys(trace) == [
        ("trace_started", None, False, False),
        ("cache_lookup", False),
        ("run_slot", True),
        ("reconciled", "HEALTHY"),
        ("cache_store", "stored"),
        ("trace_finished", "analyzed", True),
    ]
    keys = [_key(e) for e in trace.events]
    run_slot_at = keys.index(("run_slot", True))
    assert keys[run_slot_at + 1] == ("node_started", "analyze_query")
    assert keys[-3:-1] == [("node_finished", "finalize_report"), ("cache_store", "stored")]
    assert _finished(trace).response == outcome.response
    assert outcome.response.cached is False


async def test_the_run_gets_the_request_trace_on_a_copy_of_the_shared_deps(
    runtime: AppRuntime,
) -> None:
    shared: TriageDeps = runtime.deps

    await runtime.triage_gateway.triage(QUERY, None, trace=ListTrace())

    assert runtime.deps is shared
    assert isinstance(shared.trace, NullTrace)


async def test_force_refresh_records_no_cache_lookup(runtime: AppRuntime) -> None:
    trace = ListTrace()

    await runtime.triage_gateway.triage(QUERY, None, force_refresh=True, trace=trace)

    assert _gateway_keys(trace)[:2] == [
        ("trace_started", None, False, True),
        ("run_slot", True),
    ]


async def test_a_shed_request_ends_in_trace_finished_with_no_response(cache) -> None:
    runner = FakeRunner(sleep_s=10.0)
    gateway = TriageGateway(
        cache, runner, _settings(triage_max_concurrent_runs=1, triage_queue_timeout_s=0.02)
    )
    first = asyncio.create_task(gateway.triage(QUERY, None))
    await runner.started.wait()
    trace = ListTrace()

    outcome = await gateway.triage(OTHER_QUERY, None, trace=trace)
    first.cancel()

    assert outcome == Shed()
    assert [_key(e) for e in trace.events] == [
        ("trace_started", None, False, False),
        ("cache_lookup", False),
        ("run_slot", False),
        ("trace_finished", "shed", False),
    ]
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_a_deadline_records_deadline_exceeded_then_the_synthesis_fallback(cache) -> None:
    gateway = TriageGateway(cache, FakeRunner(sleep_s=10.0), _settings(triage_run_deadline_s=0.05))
    trace = ListTrace()

    outcome = await gateway.triage(QUERY, None, trace=trace)

    assert isinstance(outcome, Analyzed)
    assert [_key(e) for e in trace.events] == [
        ("trace_started", None, False, False),
        ("cache_lookup", False),
        ("run_slot", True),
        ("deadline_exceeded",),
        ("reconciled", "SEVERELY_DEGRADED"),
        ("cache_store", "skipped_fallback"),
        ("trace_finished", "analyzed", True),
    ]
    finished = _finished(trace)
    assert finished.response is not None
    assert "Run deadline exceeded" in finished.response.report.root_cause_analysis
    assert finished.response.trace is not None
    assert finished.response.trace.deadline_exceeded is True


async def test_a_deadline_closes_the_open_nodes_before_deadline_exceeded(
    runtime: AppRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "triage_run_deadline_s", 0.3)
    runtime.chaos.update_config(ChaosConfig(active=True, latency_ms=5000))
    trace = ListTrace()

    await runtime.triage_gateway.triage(QUERY, "internal", force_refresh=True, trace=trace)

    kinds = [e.kind for e in trace.events]
    deadline_at = kinds.index("deadline_exceeded")
    cancelled = [e for e in trace.events if e.kind == "node_finished" and e.status == "cancelled"]
    assert cancelled
    assert all(e.seq < deadline_at for e in cancelled)
    assert kinds[deadline_at:] == [
        "deadline_exceeded",
        "reconciled",
        "cache_store",
        "trace_finished",
    ]


async def test_an_unexpected_exception_ends_in_trace_finished_with_a_reconciled_fallback(
    cache,
) -> None:
    gateway = TriageGateway(cache, FakeRunner(raises=RuntimeError("Secret db failure")), settings)
    trace = ListTrace()

    outcome = await gateway.triage(QUERY, None, trace=trace)

    assert isinstance(outcome, Analyzed)
    assert [_key(e) for e in trace.events] == [
        ("trace_started", None, False, False),
        ("cache_lookup", False),
        ("run_slot", True),
        ("reconciled", "SEVERELY_DEGRADED"),
        ("cache_store", "skipped_fallback"),
        ("trace_finished", "analyzed", True),
    ]
    finished = _finished(trace)
    assert finished.response is not None
    assert finished.response.report.degradation_status == DegradationStatus.SEVERELY_DEGRADED
    assert all("Secret db failure" not in e.model_dump_json() for e in trace.events)


async def test_a_run_without_a_final_report_records_its_reconciled_fallback(cache) -> None:
    gateway = TriageGateway(cache, FakeRunner({"tripped_breakers": ["statuspage"]}), settings)
    trace = ListTrace()

    await gateway.triage(QUERY, None, trace=trace)

    reconciled = [e for e in trace.events if isinstance(e, Reconciled)]
    assert len(reconciled) == 1
    assert reconciled[0].changes.breakers_after == ["statuspage"]
    assert _gateway_keys(trace)[-2:] == [
        ("cache_store", "skipped_fallback"),
        ("trace_finished", "analyzed", True),
    ]


async def test_a_cancelled_run_propagates_frees_its_slot_and_records_no_trace_finished(
    cache,
) -> None:
    runner = FakeRunner(raises=asyncio.CancelledError())
    gateway = TriageGateway(cache, runner, _settings(triage_max_concurrent_runs=1))
    trace = ListTrace()

    with pytest.raises(asyncio.CancelledError):
        await gateway.triage(QUERY, None, trace=trace)

    assert "trace_finished" not in [e.kind for e in trace.events]
    runner.raises = None
    assert isinstance(await gateway.triage(QUERY, None), Analyzed)


class StoreFailingCache(SemanticCacheManager):
    """A semantic cache whose store raises."""

    async def store(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("store exploded")


async def test_a_failing_cache_store_is_recorded_and_still_answered() -> None:
    failing = StoreFailingCache(
        redis_url=UNREACHABLE_REDIS, embedding_provider=DeterministicEmbeddingProvider()
    )
    await failing.initialize()
    gateway = TriageGateway(failing, FakeRunner(), settings)
    trace = ListTrace()
    try:
        outcome = await gateway.triage(QUERY, None, trace=trace)
    finally:
        await failing.close()

    assert isinstance(outcome, Analyzed)
    assert _gateway_keys(trace)[-2:] == [
        ("cache_store", "failed"),
        ("trace_finished", "analyzed", True),
    ]


async def test_the_response_summary_is_summarized_from_the_events_before_trace_finished(
    runtime: AppRuntime,
) -> None:
    trace = ListTrace()

    outcome = await runtime.triage_gateway.triage(QUERY, None, trace=trace)

    assert isinstance(outcome, Analyzed)
    assert outcome.response.trace == summarize(trace.events[:-1])


async def test_without_a_recorder_the_response_has_no_summary(runtime: AppRuntime) -> None:
    outcome = await runtime.triage_gateway.triage(QUERY, None)

    assert isinstance(outcome, Analyzed)
    assert outcome.response.trace is None


async def test_a_failover_run_summary_counts_the_fallback_call(serve_app) -> None:
    limited = RateLimited(retry_after_s=30.0, scope="minute", detail="429")
    async with serve_app(ScriptedAdapter([limited, DEFAULT_REPORT_JSON])) as (rt, _):
        trace = ListTrace()
        outcome = await rt.triage_gateway.triage(QUERY, None, trace=trace)

    assert isinstance(outcome, Analyzed)
    summary = outcome.response.trace
    assert summary is not None
    assert [(c.role, c.outcome) for c in summary.model_calls] == [
        ("primary", "rate_limited"),
        ("fallback", "completion"),
    ]
    assert summary.failovers == 1
    assert summary.outcome == "analyzed"
    assert summary.timings.model_ms <= summary.timings.total_ms
    assert summary.timings.run_ms is not None
    assert summary.timings.run_ms <= summary.timings.total_ms


async def test_a_degraded_run_summary_shows_the_tripped_read_and_degradation(
    runtime: AppRuntime,
) -> None:
    force_open_for_tests(runtime.breakers.get_or_create("chaos"))
    trace = ListTrace()

    outcome = await runtime.triage_gateway.triage(QUERY, "internal", trace=trace)

    assert isinstance(outcome, Analyzed)
    summary = outcome.response.trace
    assert summary is not None
    assert [(r.source, r.outcome) for r in summary.telemetry] == [("chaos", "tripped")]
    assert "compensate" in summary.routes
    assert summary.degradation == outcome.report.degradation_status
    assert summary.degradation != DegradationStatus.HEALTHY


SENTINEL = "SENTINELQUERYTEXT"


async def test_no_event_but_trace_finished_carries_the_query_or_model_output(serve_app) -> None:
    adapter = ScriptedAdapter(
        [f'{{"summary": "{SENTINEL}", "{SENTINEL}": 1}}', DEFAULT_REPORT_JSON]
    )
    async with serve_app(adapter) as (rt, _):
        trace = ListTrace()
        outcome = await rt.triage_gateway.triage(f"Checkout {SENTINEL} outage", None, trace=trace)

    assert isinstance(outcome, Analyzed)
    assert trace.events[-1].kind == "trace_finished"
    assert all(SENTINEL not in e.model_dump_json() for e in trace.events[:-1])
    assert outcome.response.trace is not None
    assert SENTINEL not in outcome.response.trace.model_dump_json()


class RaisingTrace:
    """A recorder that breaks the never-raises contract on every event."""

    def record(self, event: TraceEvent) -> None:
        raise RuntimeError("recorder down")


def _comparable(outcome: CacheHit | Analyzed | Shed) -> tuple[Any, ...]:
    """An outcome's type and response, less timings, generated ids and the trace summary."""
    if isinstance(outcome, Shed):
        return (Shed,)
    response = outcome.response.model_dump(
        exclude={
            "execution_time_ms": True,
            "trace": True,
            "report": {"incident_id": True, "timestamp": True},
        }
    )
    return (type(outcome), response)


async def _gateway_outcome(scenario: str, trace: TraceRecorder) -> CacheHit | Analyzed | Shed:
    """Run one request-path scenario on a fresh in-memory cache, recording on `trace`."""
    manager = SemanticCacheManager(
        redis_url=UNREACHABLE_REDIS, embedding_provider=DeterministicEmbeddingProvider()
    )
    await manager.initialize()
    try:
        if scenario == "shed":
            runner = FakeRunner(sleep_s=10.0)
            one_slot = _settings(triage_max_concurrent_runs=1, triage_queue_timeout_s=0.02)
            gateway = TriageGateway(manager, runner, one_slot)
            first = asyncio.create_task(gateway.triage(QUERY, None))
            await runner.started.wait()
            try:
                return await gateway.triage(OTHER_QUERY, None, trace=trace)
            finally:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
        if scenario == "deadline":
            gateway = TriageGateway(
                manager, FakeRunner(sleep_s=10.0), _settings(triage_run_deadline_s=0.05)
            )
            return await gateway.triage(QUERY, None, trace=trace)
        gateway = TriageGateway(manager, FakeRunner(), settings)
        if scenario == "hit":
            await gateway.triage(QUERY, "billing")
            return await gateway.triage(QUERY, "billing", trace=trace)
        return await gateway.triage(QUERY, None, trace=trace)
    finally:
        await manager.close()


@pytest.mark.parametrize(
    ("scenario", "expected_type"),
    [("hit", CacheHit), ("analyzed", Analyzed), ("shed", Shed), ("deadline", Analyzed)],
)
async def test_a_raising_recorder_does_not_change_the_outcome(
    scenario: str, expected_type: type
) -> None:
    expected = await _gateway_outcome(scenario, NullTrace())

    actual = await _gateway_outcome(scenario, RaisingTrace())

    assert isinstance(actual, expected_type)
    assert _comparable(actual) == _comparable(expected)
    if isinstance(actual, CacheHit):
        assert isinstance(expected, CacheHit)
        assert actual.similarity == expected.similarity
