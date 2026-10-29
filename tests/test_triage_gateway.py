"""Tests for the Triage gateway: cache lookup, run slots, the only cache write, and counters."""

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings, settings
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport, SeverityLevel
from resilient_triage.triage_gateway import Analyzed, CacheHit, TriageGateway

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
