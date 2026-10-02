"""Tests for semantic caching, embeddings, and sub-20ms vector lookups."""

import asyncio
import numpy as np
import pytest

from resilient_triage.cache.embeddings import (
    DeterministicEmbeddingProvider,
    FastEmbedProvider,
)
from resilient_triage.cache.semantic_cache import (
    CacheMatch,
    SemanticCacheManager,
    format_scoped_query,
)
from resilient_triage.schemas.incident import (
    DegradationStatus,
    IncidentTriageReport,
    SeverityLevel,
)


def _make_sample_report(summary: str = "GitHub Actions failure", degraded: bool = False) -> IncidentTriageReport:
    return IncidentTriageReport(
        summary=summary,
        severity=SeverityLevel.HIGH,
        root_cause_analysis="Upstream service outage",
        degradation_status=DegradationStatus.PARTIALLY_DEGRADED if degraded else DegradationStatus.HEALTHY,
        circuit_breakers_tripped=["statuspage"] if degraded else [],
        confidence_score=0.92,
    )


def test_deterministic_embedding_provider():
    """Verify deterministic vectorizer produces L2-normalized 384-dim vectors."""
    provider = DeterministicEmbeddingProvider(dimension=384)
    assert provider.dimension == 384

    vec1 = provider.embed_query("GitHub Actions workflows are failing")
    vec2 = provider.embed_query("GitHub Actions workflows are failing")
    vec3 = provider.embed_query("Completely unrelated query about billing invoices")

    assert len(vec1) == 384
    norm1 = np.linalg.norm(vec1)
    assert np.isclose(norm1, 1.0, atol=1e-5)

    # Identical text produces identical vector
    sim_identical = float(np.dot(vec1, vec2))
    assert np.isclose(sim_identical, 1.0, atol=1e-5)

    # Dissimilar text produces low similarity
    sim_different = float(np.dot(vec1, vec3))
    assert sim_different < 0.70


def test_fastembed_provider():
    """Verify FastEmbed model produces 384-dim embeddings."""
    provider = FastEmbedProvider(model_name="BAAI/bge-small-en-v1.5")
    assert provider.dimension == 384

    vec = provider.embed_query("Test query for FastEmbed")
    assert len(vec) == 384
    assert np.isclose(np.linalg.norm(vec), 1.0, atol=1e-5)

    batch = provider.embed_documents(["Query 1", "Query 2"])
    assert len(batch) == 2
    assert len(batch[0]) == 384


def test_embedding_provider_lru_cache():
    """Verify embedding provider caches query vectors in LRU cache."""
    provider = DeterministicEmbeddingProvider(dimension=384, max_cache_size=2)
    assert len(provider._cache) == 0

    v1 = provider.embed_query("query A")
    assert len(provider._cache) == 1
    assert "query a" in [k.lower() for k in provider._cache]

    # Re-reading same query uses cache directly
    v1_cached = provider.embed_query("query A")
    assert v1 == v1_cached
    assert len(provider._cache) == 1

    # Add second query
    provider.embed_query("query B")
    assert len(provider._cache) == 2

    # Add third query: exceeds max_cache_size 2, evicts oldest
    provider.embed_query("query C")
    assert len(provider._cache) == 2
    assert "query a" not in [k.lower() for k in provider._cache]
    assert "query c" in [k.lower() for k in provider._cache]



def test_format_scoped_query():
    """Verify query scope formatting."""
    assert format_scoped_query("Is Actions down?") == "Is Actions down?"
    assert format_scoped_query("Is Actions down?", "github-actions") == "[scope: github-actions] Is Actions down?"
    assert format_scoped_query("  Is Actions down?  ", "  CI  ") == "[scope: ci] Is Actions down?"


@pytest.mark.asyncio
async def test_semantic_cache_hit_under_20ms():
    """Verify cache hit for identical query returns in sub-20ms with similarity >= 0.90."""
    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(
        similarity_threshold=0.90,
        embedding_provider=provider,
    )
    await cache.initialize()
    await cache.clear()

    report = _make_sample_report("Actions runner queue stalled")
    query = "GitHub Actions runner jobs are not picking up"

    await cache.store(query, report)

    match = await cache.lookup(query)
    assert match is not None
    assert isinstance(match, CacheMatch)
    assert match.cache_hit is True
    assert match.similarity >= 0.90
    assert match.report.summary == "Actions runner queue stalled"
    # Target is sub-20ms
    assert match.execution_time_ms < 20.0


@pytest.mark.asyncio
async def test_semantic_cache_miss_on_dissimilar_query():
    """Verify cache miss when similarity is below threshold."""
    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(
        similarity_threshold=0.90,
        embedding_provider=provider,
    )
    await cache.initialize()
    await cache.clear()

    report = _make_sample_report("Database replication lag")
    await cache.store("Database replication lag spike in us-east-1", report)

    # Completely different query
    match = await cache.lookup("DNS resolution failure on api gateway")
    assert match is None


@pytest.mark.asyncio
async def test_force_refresh_bypasses_cache():
    """Verify force_refresh=True bypasses cache lookup entirely."""
    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(embedding_provider=provider)
    await cache.initialize()
    await cache.clear()

    report = _make_sample_report("API latency spike")
    query = "High API latency on checkout"
    await cache.store(query, report)

    # Regular lookup hits
    assert await cache.lookup(query) is not None

    # force_refresh bypasses
    match = await cache.lookup(query, force_refresh=True)
    assert match is None


@pytest.mark.asyncio
async def test_service_scope_isolation():
    """Verify service_filter isolates queries with similar symptom words."""
    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(embedding_provider=provider)
    await cache.initialize()
    await cache.clear()

    report = _make_sample_report("Actions queue full")
    # Store with scope 'actions'
    await cache.store("High queue latency", report, service_filter="actions")

    # Query with same words but different scope 'billing'
    match_billing = await cache.lookup("High queue latency", service_filter="billing")
    assert match_billing is None

    # Query with matching scope 'actions' hits
    match_actions = await cache.lookup("High queue latency", service_filter="actions")
    assert match_actions is not None
    assert match_actions.similarity >= 0.90


@pytest.mark.asyncio
async def test_degraded_report_reduced_ttl():
    """Verify degraded reports receive shorter 60s TTL."""
    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(
        default_ttl=300,
        degraded_ttl=60,
        embedding_provider=provider,
    )
    await cache.initialize()
    await cache.clear()

    healthy_report = _make_sample_report("Normal incident", degraded=False)
    degraded_report = _make_sample_report("Severe outage with tripped breaker", degraded=True)

    await cache.store("Healthy query", healthy_report)
    await cache.store("Degraded query", degraded_report)

    # Check in-memory document TTLs
    entries = list(cache._in_memory_docs.values())
    assert len(entries) == 2

    healthy_entry = next(e for e in entries if "Healthy query" in e.query)
    degraded_entry = next(e for e in entries if "Degraded query" in e.query)

    import time
    now = time.monotonic()
    # Healthy TTL should be ~300s, Degraded TTL should be ~60s
    assert 280 < (healthy_entry.expires_at - now) <= 300
    assert 40 < (degraded_entry.expires_at - now) <= 60


@pytest.mark.asyncio
async def test_cache_clear():
    """Verify clear() purges all stored entries."""
    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(embedding_provider=provider)
    await cache.initialize()

    report = _make_sample_report("Outage")
    await cache.store("Test query", report)
    assert await cache.lookup("Test query") is not None

    await cache.clear()
    assert await cache.lookup("Test query") is None


@pytest.mark.asyncio
async def test_redis_connectivity_and_driver():
    """Verify live Redis connection detection and RediSearch driver reporting."""
    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(embedding_provider=provider)
    is_connected = await cache.check_connection()
    assert is_connected is True
    assert cache.is_redis_connected is True
    assert cache.driver in ("redisearch", "redis_hash_fallback")
    await cache.close()


@pytest.mark.asyncio
async def test_redis_disconnect_and_in_memory_failover():
    """Verify that if Redis drops or is unreachable, cache seamlessly falls back to in-memory mode with zero errors."""
    provider = DeterministicEmbeddingProvider()
    # Point to an unreachable port
    unreachable_cache = SemanticCacheManager(
        redis_url="redis://127.0.0.1:59999/0",
        embedding_provider=provider,
    )
    unreachable_cache.reconnect_cooldown_seconds = 0.01
    await unreachable_cache.initialize()

    assert unreachable_cache.is_redis_connected is False
    assert unreachable_cache.driver == "in_memory"

    # Store and lookup should work completely fine in in-memory mode
    report = _make_sample_report("Incident during Redis outage")
    await unreachable_cache.store("Database connection timeout", report)

    match = await unreachable_cache.lookup("Database connection timeout")
    assert match is not None
    assert match.cache_hit is True
    assert match.report.summary == "Incident during Redis outage"
    assert match.execution_time_ms < 20.0

