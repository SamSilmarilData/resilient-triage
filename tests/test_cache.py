"""Tests for semantic caching, embeddings, and sub-20ms vector lookups."""

import numpy as np
import pytest
import pytest_asyncio

from resilient_triage.cache.embeddings import (
    DeterministicEmbeddingProvider,
    FastEmbedProvider,
)
from resilient_triage.cache.semantic_cache import (
    CacheMatch,
    SemanticCacheManager,
    format_scoped_query,
    normalize_scope,
)
from resilient_triage.schemas.incident import (
    DegradationStatus,
    IncidentTriageReport,
    SeverityLevel,
)


@pytest_asyncio.fixture(autouse=True)
async def cleanup_cache_instances():
    """Track and close SemanticCacheManager instances created in tests to prevent socket leaks."""
    created: list[SemanticCacheManager] = []
    original_init = SemanticCacheManager.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    SemanticCacheManager.__init__ = tracked_init
    try:
        yield
    finally:
        SemanticCacheManager.__init__ = original_init
        for c in created:
            try:
                await c.close()
            except Exception:
                pass


def _make_sample_report(
    summary: str = "GitHub Actions failure", degraded: bool = False
) -> IncidentTriageReport:
    return IncidentTriageReport(
        summary=summary,
        severity=SeverityLevel.HIGH,
        root_cause_analysis="Upstream service outage",
        degradation_status=DegradationStatus.PARTIALLY_DEGRADED
        if degraded
        else DegradationStatus.HEALTHY,
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
    assert (
        format_scoped_query("Is Actions down?", "github-actions")
        == "[scope: github-actions] Is Actions down?"
    )
    assert format_scoped_query("  Is Actions down?  ", "  CI  ") == "[scope: ci] Is Actions down?"


def test_normalize_scope():
    """Verify scopes are stripped, lower-cased and reduced to [a-z0-9_-], and None means none."""
    assert normalize_scope(None) == ""
    assert normalize_scope("   ") == ""
    assert normalize_scope("  GitHub-Actions ") == "github-actions"
    assert normalize_scope("api_gw (prod)!") == "api_gwprod"
    assert normalize_scope("{*}") == ""


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
    entries = list(cache._standby._docs.values())
    assert len(entries) == 2

    healthy_entry = next(e for e in entries if "Healthy query" in e.entry.query)
    degraded_entry = next(e for e in entries if "Degraded query" in e.entry.query)

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
    if not is_connected:
        pytest.skip("Local Redis server is not reachable on localhost:6379 in this environment")
    assert is_connected is True
    assert cache.is_redis_connected is True
    assert cache.driver in ("redisearch", "redis_hash_fallback")
    await cache.close()


@pytest.mark.asyncio
async def test_redis_disconnect_and_in_memory_failover():
    """Verify the cache falls back to in-memory without errors if Redis drops or is unreachable."""
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


@pytest.mark.asyncio
async def test_exact_scope_partitioning_prevents_false_cache_hits():
    """Verify identical queries under different scopes do not cross-contaminate."""
    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(embedding_provider=provider)
    await cache.initialize()
    await cache.clear()

    actions_report = _make_sample_report("GitHub Actions queue stalled")
    billing_report = _make_sample_report("Stripe webhook queue stalled")

    query = "Queue latency is critically high"
    await cache.store(query, actions_report, service_filter="actions")
    await cache.store(query, billing_report, service_filter="billing")

    # actions scope lookup returns actions report
    match_actions = await cache.lookup(query, service_filter="actions")
    assert match_actions is not None
    assert match_actions.report.summary == "GitHub Actions queue stalled"

    # billing scope lookup returns billing report
    match_billing = await cache.lookup(query, service_filter="billing")
    assert match_billing is not None
    assert match_billing.report.summary == "Stripe webhook queue stalled"

    # unrelated scope lookup yields MISS
    match_unrelated = await cache.lookup(query, service_filter="database")
    assert match_unrelated is None


@pytest.mark.asyncio
async def test_in_memory_cache_capacity_eviction():
    """Verify that in-memory store evicts oldest entry when max_capacity is reached."""
    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(
        redis_url="redis://127.0.0.1:59999/0",
        embedding_provider=provider,
        max_capacity=2,
    )
    cache.reconnect_cooldown_seconds = 0.01
    await cache.initialize()
    await cache.clear()

    r1 = _make_sample_report("Report 1")
    r2 = _make_sample_report("Report 2")
    r3 = _make_sample_report("Report 3")

    await cache.store("query 1", r1)
    await cache.store("query 2", r2)
    assert cache.count == 2

    # Storing 3rd entry triggers eviction of oldest
    await cache.store("query 3", r3)
    assert cache.count == 2

    # Query 1 was evicted
    assert await cache.lookup("query 1") is None
    # Query 3 is present
    assert await cache.lookup("query 3") is not None


@pytest.mark.asyncio
async def test_redisearch_query_syntax_error_does_not_disconnect_redis(mocker):
    """Verify that RediSearch query syntax errors do not drop the Redis connection."""
    import redis.exceptions

    provider = DeterministicEmbeddingProvider()
    cache = SemanticCacheManager(embedding_provider=provider)
    cache._initialized = True

    # Simulate connected Redis with RediSearch module
    mock_redis = mocker.AsyncMock()
    mock_ft = mocker.MagicMock()
    mock_ft.search = mocker.AsyncMock(
        side_effect=redis.exceptions.ResponseError("Syntax error at offset 5")
    )
    mock_redis.ft = mocker.MagicMock(return_value=mock_ft)
    cache._redis = mock_redis
    cache._has_redisearch = True

    # Pre-populate in-memory fallback
    report = _make_sample_report("Fallback incident")
    await cache.store("Special query", report, service_filter="billing")

    # Lookup triggers RediSearch ResponseError
    match = await cache.lookup("Special query", service_filter="billing")

    # Redis must NOT be disconnected by a query execution syntax error
    assert cache._redis is not None
    assert cache.is_redis_connected is True
    # In-memory search successfully served the match as fallback
    assert match is not None
    assert match.report.summary == "Fallback incident"


def test_fastembed_fallback_to_deterministic_when_offline(mocker):
    """Verify FastEmbedProvider falls back to DeterministicEmbeddingProvider if init fails."""
    provider = FastEmbedProvider(model_name="nonexistent/model")
    mocker.patch(
        "fastembed.TextEmbedding", side_effect=RuntimeError("Offline network error: 403 Forbidden")
    )

    vec = provider.embed_query("Fallback test query")
    assert len(vec) == 384
    assert np.isclose(np.linalg.norm(vec), 1.0, atol=1e-5)
    assert provider._fallback is not None

    batch = provider.embed_documents(["Query 1", "Query 2"])
    assert len(batch) == 2
    assert len(batch[0]) == 384
    provider.warmup()


class _RaisingEmbeddingProvider(DeterministicEmbeddingProvider):
    """Embedding provider whose query embedding always fails, like a crashed ONNX runtime."""

    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("onnxruntime: bad alloc")


async def test_embedding_failure_degrades_lookup_to_miss_and_skips_store(caplog):
    """An embedding failure makes lookup a logged MISS and store a logged no-op, never a raise."""
    cache = SemanticCacheManager(embedding_provider=_RaisingEmbeddingProvider())
    await cache.initialize()
    await cache.clear()
    caplog.set_level("WARNING", logger="resilient_triage.cache.semantic_cache")

    await cache.store("GitHub Actions runner jobs are not picking up", _make_sample_report())
    assert cache.count == 0
    assert "bad alloc" in caplog.text

    caplog.clear()
    assert await cache.lookup("GitHub Actions runner jobs are not picking up") is None
    assert "bad alloc" in caplog.text


def _in_memory_cache(**kwargs) -> SemanticCacheManager:
    """A manager whose Redis is unreachable, so every read is served by the in-memory standby."""
    return SemanticCacheManager(
        redis_url="redis://127.0.0.1:59999/0",
        embedding_provider=DeterministicEmbeddingProvider(),
        **kwargs,
    )


async def test_corrupt_cached_report_is_a_miss_and_is_evicted(caplog):
    """A cached report that no longer validates is a logged MISS and is dropped from the cache."""
    cache = _in_memory_cache()
    await cache.initialize()
    query = "GitHub Actions runner jobs are not picking up"
    # model_construct skips validation, so this dumps JSON that IncidentTriageReport rejects
    # (summary is shorter than min_length=5), like a hash written under an older schema.
    corrupt = IncidentTriageReport.model_construct(**_make_sample_report().model_dump())
    corrupt.summary = "x"
    await cache.store(query, corrupt)
    assert cache.count == 1
    caplog.set_level("WARNING", logger="resilient_triage.cache.semantic_cache")

    assert await cache.lookup(query) is None
    assert cache.count == 0
    assert "summary" in caplog.text

    await cache.store(query, _make_sample_report("Actions runner queue stalled"))
    match = await cache.lookup(query)
    assert match is not None
    assert match.report.summary == "Actions runner queue stalled"


class _WrongDimensionForOneQuery(DeterministicEmbeddingProvider):
    """Embeds normally, except one query comes back 768-d, like a swapped-in embedding model."""

    BAD_QUERY = "Webhook deliveries are delayed"

    def embed_query(self, text: str) -> list[float]:
        if text == self.BAD_QUERY:
            return [1.0 / np.sqrt(768)] * 768
        return super().embed_query(text)


async def test_wrong_dimension_vector_is_rejected_and_lookups_keep_working(caplog):
    """A vector whose size isn't the provider's is never cached, and other lookups still hit."""
    cache = SemanticCacheManager(
        redis_url="redis://127.0.0.1:59999/0",
        embedding_provider=_WrongDimensionForOneQuery(),
    )
    await cache.initialize()
    good_query = "GitHub Actions runner jobs are not picking up"
    await cache.store(good_query, _make_sample_report("Actions runner queue stalled"))
    caplog.set_level("WARNING", logger="resilient_triage.cache.semantic_cache")

    await cache.store(_WrongDimensionForOneQuery.BAD_QUERY, _make_sample_report())

    assert cache.count == 1
    assert "768" in caplog.text
    match = await cache.lookup(good_query)
    assert match is not None
    assert match.report.summary == "Actions runner queue stalled"
    assert await cache.lookup(_WrongDimensionForOneQuery.BAD_QUERY) is None
