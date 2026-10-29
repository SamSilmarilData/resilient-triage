"""Tests for the Triage gateway: cache lookup, run slots, the only cache write, and counters."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings, settings
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport, SeverityLevel
from resilient_triage.triage_gateway import (
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

    async def ainvoke(self, input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(input)
        return self.state


class BlockingGraph:
    """A GraphRunner whose runs wait until `release` is set, then return a report."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def ainvoke(self, input: dict[str, Any]) -> dict[str, Any]:
        self.started.set()
        await self.release.wait()
        return {"final_report": _report(f"Answer for {input['query']}")}


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


def _one_slot(timeout_s: float = 0.05) -> Settings:
    return settings.model_copy(
        update={"triage_max_concurrent_runs": 1, "triage_queue_timeout_s": timeout_s}
    )


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

    assert outcome == Analyzed(report=fallback)
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
