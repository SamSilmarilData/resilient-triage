"""Tests for the Triage gateway: cache lookup, run slots, the only cache write, and counters."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.cache.in_memory_store import InMemoryVectorStore
from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.cache.vector_store import VectorEntry
from resilient_triage.config import Settings, settings
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport, SeverityLevel
from resilient_triage.triage.gateway import (
    Analyzed,
    CacheHit,
    Shed,
    TriageGateway,
    TriageMetrics,
)

UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
QUERY = "Payments API returning 503 for card checkouts"


def _report(summary: str = "Payments API returning 503") -> IncidentTriageReport:
    return IncidentTriageReport(
        summary=summary,
        severity=SeverityLevel.HIGH,
        root_cause_analysis="Upstream payment processor outage",
        degradation_status=DegradationStatus.HEALTHY,
        circuit_breakers_tripped=[],
        confidence_score=0.9,
    )


class ReturningGraph:
    """A GraphRunner that returns a given final state and records each input."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, input: dict[str, Any], *, trace: Any = None) -> dict[str, Any]:
        self.calls.append(input)
        return self.state


class BlockingGraph:
    """A GraphRunner whose runs wait until `release` is set, then return a report."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def ainvoke(self, input: dict[str, Any], *, trace: Any = None) -> dict[str, Any]:
        self.started.set()
        await self.release.wait()
        return {"final_report": _report(f"Answer for {input['query']}")}


class RaisingGraph:
    """A GraphRunner whose runs raise the given exception."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def ainvoke(self, input: dict[str, Any], *, trace: Any = None) -> dict[str, Any]:
        raise self.exc


@pytest_asyncio.fixture
async def cache() -> AsyncIterator[SemanticCacheManager]:
    """An in-memory semantic cache (Redis unreachable) with offline embeddings."""
    manager = SemanticCacheManager(
        redis_url=UNREACHABLE_REDIS, embedding_provider=DeterministicEmbeddingProvider()
    )
    await manager.initialize()
    yield manager
    await manager.close()


def _gateway(
    cache: SemanticCacheManager, graph: Any, app_settings: Settings = settings
) -> TriageGateway:
    return TriageGateway(cache, graph.ainvoke, app_settings)


async def test_a_cache_hit_returns_the_cached_report_without_running_the_graph(cache):
    await cache.store(QUERY, _report("Cached answer"), service_filter="billing")
    graph = ReturningGraph({"final_report": _report("Fresh answer")})

    outcome = await _gateway(cache, graph).triage(QUERY, "billing")

    assert isinstance(outcome, CacheHit)
    assert outcome.report.summary == "Cached answer"
    assert outcome.similarity >= settings.cache_similarity_threshold
    assert graph.calls == []


async def test_force_refresh_skips_the_lookup_and_runs_the_graph(cache):
    await cache.store(QUERY, _report("Cached answer"), service_filter="billing")
    graph = ReturningGraph({"final_report": _report("Fresh answer")})

    outcome = await _gateway(cache, graph).triage(QUERY, "billing", force_refresh=True)

    assert isinstance(outcome, Analyzed)
    assert outcome.report.summary == "Fresh answer"
    assert graph.calls == [{"query": QUERY, "service_filter": "billing"}]


async def test_a_miss_stores_the_report_under_its_scope(cache):
    graph = ReturningGraph({"final_report": _report("Fresh answer")})

    outcome = await _gateway(cache, graph).triage(QUERY, "billing")

    assert isinstance(outcome, Analyzed)
    stored = await cache.lookup(QUERY, service_filter="billing")
    assert stored is not None
    assert stored.report == outcome.report
    assert await cache.lookup(QUERY, service_filter="actions") is None


async def test_a_run_without_a_final_report_answers_with_an_unstored_synthesis_fallback(cache):
    graph = ReturningGraph({"tripped_breakers": ["statuspage"]})

    outcome = await _gateway(cache, graph).triage(QUERY, None)

    assert isinstance(outcome, Analyzed)
    assert outcome.report.summary == f"Automated incident triage degraded for: {QUERY}"
    assert outcome.report.severity == SeverityLevel.UNKNOWN
    assert outcome.report.degradation_status == DegradationStatus.SEVERELY_DEGRADED
    assert outcome.report.circuit_breakers_tripped == ["statuspage"]
    assert outcome.report.root_cause_analysis == (
        "Self-repair limits reached. Diagnostic context: LLM synthesis error"
    )
    assert await cache.lookup(QUERY) is None


def _one_slot(timeout_s: float = 0.05, *, deadline_s: float | None = None) -> Settings:
    update: dict[str, Any] = {"triage_max_concurrent_runs": 1, "triage_queue_timeout_s": timeout_s}
    if deadline_s is not None:
        update["triage_run_deadline_s"] = deadline_s
    return settings.model_copy(update=update)


async def test_a_request_with_no_free_run_slot_is_shed_after_the_queue_timeout(cache):
    graph = BlockingGraph()
    gateway = _gateway(cache, graph, _one_slot(timeout_s=0.05))
    first = asyncio.create_task(gateway.triage(QUERY, None))
    await graph.started.wait()

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    outcome = await asyncio.wait_for(
        gateway.triage("Checkout latency spike in eu-west-1", None), timeout=1.0
    )

    assert outcome == Shed()
    assert loop.time() - started_at >= 0.05
    graph.release.set()
    assert isinstance(await first, Analyzed)


async def test_the_run_slot_is_freed_once_the_run_finishes(cache):
    graph = BlockingGraph()
    gateway = _gateway(cache, graph, _one_slot())
    first = asyncio.create_task(gateway.triage(QUERY, None))
    await graph.started.wait()
    graph.release.set()
    await first

    outcome = await gateway.triage("Checkout latency spike in eu-west-1", None)

    assert isinstance(outcome, Analyzed)


async def test_a_graph_exception_answers_with_an_unstored_synthesis_fallback(cache):
    gateway = _gateway(cache, RaisingGraph(RuntimeError("Secret internal db failure")), _one_slot())

    outcome = await gateway.triage(QUERY, "billing")

    assert isinstance(outcome, Analyzed)
    assert outcome.report.summary == f"Automated incident triage degraded for: {QUERY}"
    assert outcome.report.severity == SeverityLevel.UNKNOWN
    assert outcome.report.degradation_status == DegradationStatus.SEVERELY_DEGRADED
    assert outcome.report.circuit_breakers_tripped == []
    assert "Secret internal db failure" not in outcome.report.model_dump_json()
    assert await cache.lookup(QUERY, service_filter="billing") is None
    assert gateway.metrics == TriageMetrics(hit=0, miss=1, saturated=0)


async def test_a_graph_exception_frees_its_run_slot(cache):
    graph = RaisingGraph(RuntimeError("boom"))
    gateway = _gateway(cache, graph, _one_slot())
    await gateway.triage(QUERY, None)

    graph.exc = RuntimeError("boom again")
    outcome = await gateway.triage("Checkout latency spike in eu-west-1", None)

    assert isinstance(outcome, Analyzed)


async def test_a_cancelled_graph_run_propagates_and_frees_its_run_slot(cache):
    graph = RaisingGraph(asyncio.CancelledError())
    gateway = _gateway(cache, graph, _one_slot())

    with pytest.raises(asyncio.CancelledError):
        await gateway.triage(QUERY, None)

    assert await cache.lookup(QUERY) is None
    assert gateway.metrics == TriageMetrics(hit=0, miss=0, saturated=0, abandoned=1)
    graph.exc = RuntimeError("boom")
    assert isinstance(await gateway.triage(QUERY, None), Analyzed)


async def test_metrics_count_each_outcome(cache):
    graph = BlockingGraph()
    gateway = _gateway(cache, graph, _one_slot())
    assert gateway.metrics == TriageMetrics(hit=0, miss=0, saturated=0)

    first = asyncio.create_task(gateway.triage(QUERY, None))
    await graph.started.wait()
    await gateway.triage("Checkout latency spike in eu-west-1", None)  # shed
    graph.release.set()
    await first  # miss
    await gateway.triage(QUERY, None)  # hit
    await gateway.triage(QUERY, None, force_refresh=True)  # miss

    assert gateway.metrics == TriageMetrics(hit=1, miss=2, saturated=1)


async def test_metrics_are_a_snapshot(cache):
    gateway = _gateway(cache, ReturningGraph({"final_report": _report()}))
    before = gateway.metrics

    await gateway.triage(QUERY, None)

    assert before == TriageMetrics(hit=0, miss=0, saturated=0)
    assert gateway.metrics.miss == 1


def _fallback_report() -> IncidentTriageReport:
    return IncidentTriageReport(
        summary="Automated incident triage degraded",
        severity=SeverityLevel.UNKNOWN,
        root_cause_analysis="Self-repair limits reached. Diagnostic context: LLM synthesis error",
        degradation_status=DegradationStatus.SEVERELY_DEGRADED,
        circuit_breakers_tripped=[],
        confidence_score=0.2,
    )


async def test_a_synthesis_fallback_is_answered_but_not_stored(cache):
    fallback = _fallback_report()
    graph = ReturningGraph({"final_report": fallback, "synthesis_fell_back": True})
    gateway = _gateway(cache, graph)

    outcome = await gateway.triage(QUERY, None)

    assert isinstance(outcome, Analyzed)
    assert outcome.report == fallback
    assert await cache.lookup(QUERY) is None
    assert gateway.metrics == TriageMetrics(hit=0, miss=1, saturated=0)

    again = await gateway.triage(QUERY, None)

    assert isinstance(again, Analyzed)
    assert len(graph.calls) == 2
    assert gateway.metrics == TriageMetrics(hit=0, miss=2, saturated=0)


async def test_a_telemetry_degraded_report_is_stored_with_the_degraded_ttl():
    now = [1000.0]
    cache = SemanticCacheManager(
        redis_url=UNREACHABLE_REDIS,
        embedding_provider=DeterministicEmbeddingProvider(),
        default_ttl=300,
        degraded_ttl=60,
        clock=lambda: now[0],
    )
    await cache.initialize()
    degraded = _report().model_copy(
        update={
            "degradation_status": DegradationStatus.PARTIALLY_DEGRADED,
            "circuit_breakers_tripped": ["statuspage"],
        }
    )
    graph = ReturningGraph({"final_report": degraded, "tripped_breakers": ["statuspage"]})
    try:
        await _gateway(cache, graph).triage(QUERY, None)

        now[0] += 59
        stored = await cache.lookup(QUERY)
        assert stored is not None
        assert stored.report == degraded
        now[0] += 2
        assert await cache.lookup(QUERY) is None
    finally:
        await cache.close()


class SleepingGraph:
    """A GraphRunner whose runs set `started`, sleep for `seconds`, then return a report."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.started = asyncio.Event()

    async def ainvoke(self, input: dict[str, Any], *, trace: Any = None) -> dict[str, Any]:
        self.started.set()
        await asyncio.sleep(self.seconds)
        return {"final_report": _report(f"Answer for {input['query']}")}


async def test_a_run_past_its_deadline_answers_with_an_unstored_synthesis_fallback(cache):
    gateway = _gateway(cache, SleepingGraph(10.0), _one_slot(deadline_s=0.05))

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    outcome = await asyncio.wait_for(gateway.triage(QUERY, "billing"), timeout=1.0)

    assert loop.time() - started_at < 0.5
    assert isinstance(outcome, Analyzed)
    assert outcome.report.summary == f"Automated incident triage degraded for: {QUERY}"
    assert outcome.report.severity == SeverityLevel.UNKNOWN
    assert outcome.report.degradation_status == DegradationStatus.SEVERELY_DEGRADED
    assert "Run deadline exceeded" in outcome.report.root_cause_analysis
    assert await cache.lookup(QUERY, service_filter="billing") is None
    assert gateway.metrics == TriageMetrics(hit=0, miss=1, saturated=0)


async def test_a_run_past_its_deadline_frees_its_run_slot(cache):
    graph = SleepingGraph(10.0)
    gateway = _gateway(cache, graph, _one_slot(deadline_s=0.05))
    await gateway.triage(QUERY, None)

    graph.seconds = 0.0
    outcome = await gateway.triage("Checkout latency spike in eu-west-1", None)

    assert isinstance(outcome, Analyzed)
    assert outcome.report.summary == "Answer for Checkout latency spike in eu-west-1"


async def test_a_run_under_its_deadline_is_answered_and_stored(cache):
    gateway = _gateway(cache, SleepingGraph(0.01), _one_slot(deadline_s=1.0))

    outcome = await gateway.triage(QUERY, "billing")

    assert isinstance(outcome, Analyzed)
    assert outcome.report.summary == f"Answer for {QUERY}"
    stored = await cache.lookup(QUERY, service_filter="billing")
    assert stored is not None
    assert stored.report == outcome.report


async def test_waiting_for_a_run_slot_does_not_count_against_the_deadline(cache):
    deadline_s = 0.3
    graph = SleepingGraph(0.2)
    gateway = _gateway(cache, graph, _one_slot(timeout_s=1.0, deadline_s=deadline_s))
    first = asyncio.create_task(gateway.triage(QUERY, None))
    await graph.started.wait()

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    second = await gateway.triage("Checkout latency spike in eu-west-1", None)

    assert loop.time() - started_at > deadline_s
    assert isinstance(second, Analyzed)
    assert second.report.summary == "Answer for Checkout latency spike in eu-west-1"
    assert isinstance(await first, Analyzed)


async def test_a_caller_cancelling_a_run_propagates_and_frees_its_run_slot(cache):
    graph = BlockingGraph()
    gateway = _gateway(cache, graph, _one_slot(deadline_s=5.0))
    task = asyncio.create_task(gateway.triage(QUERY, None))
    await graph.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await cache.lookup(QUERY) is None
    assert gateway.metrics == TriageMetrics(hit=0, miss=0, saturated=0, abandoned=1)
    graph.release.set()
    outcome = await gateway.triage("Checkout latency spike in eu-west-1", None)
    assert isinstance(outcome, Analyzed)
    assert outcome.report.summary == "Answer for Checkout latency spike in eu-west-1"


async def test_a_timeout_raised_inside_the_graph_is_a_graph_exception_not_the_deadline(cache):
    gateway = _gateway(
        cache, RaisingGraph(TimeoutError("upstream read timed out")), _one_slot(deadline_s=5.0)
    )

    outcome = await gateway.triage(QUERY, None)

    assert isinstance(outcome, Analyzed)
    assert outcome.report.degradation_status == DegradationStatus.SEVERELY_DEGRADED
    assert "failed unexpectedly" in outcome.report.root_cause_analysis
    assert "Run deadline" not in outcome.report.root_cause_analysis


async def test_a_stream_left_before_its_run_starts_counts_one_abandoned_request(cache):
    graph = ReturningGraph({"final_report": _report()})
    gateway = _gateway(cache, graph)

    async with gateway.stream(QUERY, None):
        pass  # left before the run task takes its first step: triage() never starts

    assert graph.calls == []
    assert gateway.metrics == TriageMetrics(abandoned=1)
    assert await cache.lookup(QUERY) is None


async def test_a_stream_left_mid_run_counts_one_abandoned_request_and_frees_its_slot(cache):
    graph = BlockingGraph()
    gateway = _gateway(cache, graph, _one_slot(deadline_s=5.0))

    async with gateway.stream(QUERY, None):
        await graph.started.wait()

    assert gateway.metrics == TriageMetrics(abandoned=1)
    assert await cache.lookup(QUERY) is None
    graph.release.set()
    assert isinstance(await gateway.triage("Checkout latency spike in eu-west-1", None), Analyzed)


class StallingRedis(InMemoryVectorStore):
    """A hash-mode remote store whose put writes, then waits for a reply that never comes."""

    def __init__(self) -> None:
        super().__init__()
        self.put_started = asyncio.Event()

    @property
    def supports_search(self) -> bool:
        return False

    async def connect(self) -> None:
        pass

    async def ping(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def entries(self) -> AsyncIterator[tuple[VectorEntry, float]]:
        for stored in list(self._docs.values()):
            yield stored.entry, 60.0

    async def put(self, entry: VectorEntry, ttl: float) -> None:
        await super().put(entry, ttl)
        self.put_started.set()
        await asyncio.Event().wait()


async def test_a_run_cancelled_during_its_cache_store_caches_nothing():
    redis = StallingRedis()
    cache = SemanticCacheManager(
        embedding_provider=DeterministicEmbeddingProvider(), remote_store_factory=lambda: redis
    )
    gateway = _gateway(cache, ReturningGraph({"final_report": _report()}))
    task = asyncio.create_task(gateway.triage(QUERY, None))
    await asyncio.wait_for(redis.put_started.wait(), timeout=5.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await cache.lookup(QUERY) is None
    assert redis.count == 0
    assert gateway.metrics == TriageMetrics(abandoned=1)


class CancelSwallowingGraph:
    """A GraphRunner that, once cancelled, swallows the cancellation and returns a report."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def ainvoke(self, input: dict[str, Any], *, trace: Any = None) -> dict[str, Any]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        return {"final_report": _report()}


async def test_a_cancelled_run_stores_nothing_even_if_the_graph_swallows_the_cancel(cache):
    graph = CancelSwallowingGraph()
    gateway = _gateway(cache, graph, _one_slot(deadline_s=5.0))
    task = asyncio.create_task(gateway.triage(QUERY, None))
    await graph.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await cache.lookup(QUERY) is None
    assert gateway.metrics == TriageMetrics(abandoned=1)


class SlowToCancelGraph:
    """A GraphRunner whose runs, once cancelled, finish cancelling only when `unwind` is set."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.unwind = asyncio.Event()

    async def ainvoke(self, input: dict[str, Any], *, trace: Any = None) -> dict[str, Any]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.unwind.wait()
            raise
        raise AssertionError("unreachable")


async def test_a_stream_cancelled_again_while_its_run_unwinds_waits_and_keeps_that_cancel(cache):
    graph = SlowToCancelGraph()
    gateway = _gateway(cache, graph, _one_slot(deadline_s=5.0))

    async def consume() -> None:
        async with gateway.stream(QUERY, None):
            await asyncio.Event().wait()

    consumer = asyncio.create_task(consume())
    await graph.started.wait()
    consumer.cancel("client disconnected")
    await graph.cancelled.wait()  # the stream now waits for its run to finish cancelling
    consumer.cancel("server shutting down")
    await asyncio.sleep(0)
    assert not consumer.done()  # still waiting for the run

    graph.unwind.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await consumer

    assert cancelled.value.args == ("server shutting down",)
    assert gateway.metrics == TriageMetrics(abandoned=1)
