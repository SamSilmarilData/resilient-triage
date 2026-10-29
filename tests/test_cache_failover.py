"""SemanticCacheManager failover: one _mark_down path, reconnect cooldown, never-raise.

The remote store is a scripted fake over InMemoryVectorStore that raises StoreUnavailable or
StoreQueryError on demand, so these tests drive the manager only through its public interface.
"""

from collections.abc import AsyncIterator

import numpy as np
import pytest

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.cache.in_memory_store import InMemoryVectorStore
from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.cache.vector_store import (
    StoreQueryError,
    StoreUnavailable,
    VectorEntry,
    VectorHit,
)
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport, SeverityLevel

QUERY = "GitHub Actions runner jobs are not picking up"


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ScriptedStore:
    """A remote VectorStore whose data survives reconnects and whose failures are scripted.

    `down` makes every call raise StoreUnavailable; `fail` maps an operation name to the
    exception that operation raises next (and every time after, until removed).
    """

    def __init__(self, *, searchable: bool = True) -> None:
        self.data = InMemoryVectorStore()
        self.searchable = searchable
        self.down = False
        self.fail: dict[str, Exception] = {}
        self.connects = 0
        self.acloses = 0
        self.searches = 0

    def _check(self, operation: str) -> None:
        if self.down:
            raise StoreUnavailable(f"scripted outage during {operation}")
        if operation in self.fail:
            raise self.fail[operation]

    @property
    def supports_search(self) -> bool:
        return self.searchable

    @property
    def count(self) -> int:
        return self.data.count

    async def connect(self) -> None:
        self.connects += 1
        self._check("connect")

    async def ping(self) -> None:
        self._check("ping")

    async def aclose(self) -> None:
        self.acloses += 1

    async def search(self, vector: np.ndarray, scope: str, threshold: float) -> VectorHit | None:
        self.searches += 1
        self._check("search")
        return await self.data.search(vector, scope, threshold)

    async def put(self, entry: VectorEntry, ttl: float) -> None:
        self._check("put")
        await self.data.put(entry, ttl)

    async def delete(self, doc_id: str) -> None:
        self._check("delete")
        await self.data.delete(doc_id)

    async def clear(self) -> None:
        self._check("clear")
        await self.data.clear()

    async def entries(self) -> AsyncIterator[tuple[VectorEntry, float]]:
        self._check("scan")
        for stored in list(self.data._docs.values()):
            yield stored.entry, 60.0


def _report(summary: str = "Actions runner queue stalled") -> IncidentTriageReport:
    return IncidentTriageReport(
        summary=summary,
        severity=SeverityLevel.HIGH,
        root_cause_analysis="Upstream service outage",
        degradation_status=DegradationStatus.HEALTHY,
        confidence_score=0.92,
    )


def _cache(remote: ScriptedStore, clock: FakeClock) -> SemanticCacheManager:
    return SemanticCacheManager(
        embedding_provider=DeterministicEmbeddingProvider(),
        remote_store_factory=lambda: remote,
        clock=clock,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


async def test_searchable_remote_serves_reads(clock):
    """With Redis up and searchable, lookups go to Redis search and the driver says so."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()
    await cache.store(QUERY, _report())

    match = await cache.lookup(QUERY)

    assert cache.driver == "redisearch"
    assert match is not None and match.report.summary == "Actions runner queue stalled"
    assert remote.searches == 1
    assert remote.count == 1


async def test_unavailable_search_fails_over_to_standby_and_closes_client(clock):
    """A connection-class search failure drops Redis once, aclose()s it, and serves the standby."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()
    await cache.store(QUERY, _report())
    remote.down = True

    match = await cache.lookup(QUERY)

    assert match is not None and match.report.summary == "Actions runner queue stalled"
    assert cache.driver == "in_memory"
    assert remote.acloses == 1


async def test_no_reconnect_during_cooldown(clock):
    """After a drop, no reconnect is attempted until the 5s cooldown has passed."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()
    remote.down = True
    await cache.lookup(QUERY)
    remote.down = False
    connects = remote.connects

    clock.now += cache.reconnect_cooldown_seconds - 1
    await cache.lookup(QUERY)
    await cache.store(QUERY, _report())

    assert remote.connects == connects
    assert cache.driver == "in_memory"


async def test_reconnects_after_cooldown(clock):
    """Once the cooldown has passed, the next call reconnects and Redis serves again."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()
    remote.down = True
    await cache.lookup(QUERY)
    remote.down = False

    clock.now += cache.reconnect_cooldown_seconds + 1
    await cache.lookup(QUERY)

    assert cache.driver == "redisearch"
    assert remote.connects == 2


async def test_failed_reconnect_restarts_cooldown_and_closes_client(clock):
    """A reconnect that fails closes the half-open client and waits another cooldown."""
    remote = ScriptedStore()
    remote.down = True
    cache = _cache(remote, clock)
    await cache.initialize()

    assert cache.driver == "in_memory"
    assert remote.acloses == 1

    clock.now += cache.reconnect_cooldown_seconds + 1
    await cache.lookup(QUERY)
    assert remote.connects == 2
    assert remote.acloses == 2

    await cache.lookup(QUERY)
    assert remote.connects == 2


async def test_query_error_does_not_disconnect(clock):
    """A StoreQueryError on search serves the standby and keeps Redis connected."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()
    await cache.store("Special query", _report("Fallback incident"), service_filter="billing")
    remote.fail["search"] = StoreQueryError("Syntax error at offset 5")

    match = await cache.lookup("Special query", service_filter="billing")

    assert match is not None and match.report.summary == "Fallback incident"
    assert cache.driver == "redisearch"
    assert remote.acloses == 0


async def test_query_error_on_write_does_not_disconnect(clock):
    """A StoreQueryError on put or clear is logged; the standby keeps the entry, Redis stays up."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()
    remote.fail["put"] = StoreQueryError("WRONGTYPE")
    remote.fail["clear"] = StoreQueryError("WRONGTYPE")

    await cache.store(QUERY, _report())
    assert cache.count == 1
    await cache.clear()

    assert cache.count == 0
    assert cache.driver == "redisearch"
    assert remote.acloses == 0


@pytest.mark.parametrize("operation", ["put", "clear", "ping"])
async def test_unavailable_on_any_operation_drops_through_mark_down(clock, operation):
    """Store, clear and the health ping all drop Redis the same way: aclose() plus cooldown."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()
    remote.fail[operation] = StoreUnavailable("connection reset")

    if operation == "put":
        await cache.store(QUERY, _report())
    elif operation == "clear":
        await cache.clear()
    else:
        assert await cache.check_connection() is False

    assert cache.driver == "in_memory"
    assert remote.acloses == 1
    del remote.fail[operation]
    await cache.lookup(QUERY)
    assert remote.connects == 1  # still cooling down


@pytest.mark.parametrize(
    "error", [StoreUnavailable("down"), StoreQueryError("bad"), RuntimeError("bug")]
)
async def test_lookup_store_clear_never_raise(clock, error):
    """Whatever the remote raises, lookup is a MISS or standby hit and store/clear are no-ops."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()
    remote.fail.update({op: error for op in ("search", "put", "clear", "ping", "connect", "scan")})

    await cache.store(QUERY, _report())
    match = await cache.lookup(QUERY)
    await cache.clear()
    miss = await cache.lookup(QUERY)
    healthy = await cache.check_connection()

    assert match is not None
    assert miss is None
    assert isinstance(healthy, bool)


async def test_hash_mode_hydrates_standby_and_never_searches_redis(clock):
    """Without search, the driver is redis_hash_fallback and reads use the hydrated standby."""
    remote = ScriptedStore(searchable=False)
    seeder = _cache(remote, clock)
    await seeder.initialize()
    await seeder.store(QUERY, _report("Persisted in Redis"))
    await seeder.close()

    cache = _cache(remote, clock)
    await cache.initialize()
    match = await cache.lookup(QUERY)

    assert cache.driver == "redis_hash_fallback"
    assert cache.count == 1
    assert match is not None and match.report.summary == "Persisted in Redis"
    assert remote.searches == 0


async def test_close_releases_client_and_reinitialises(clock):
    """close() aclose()s the client; the next call reconnects without waiting for a cooldown."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()

    await cache.close()

    assert remote.acloses == 1
    assert cache.driver == "in_memory"
    await cache.lookup(QUERY)
    assert remote.connects == 2
    assert cache.driver == "redisearch"


async def test_invalid_report_from_redis_search_is_a_miss_and_evicted(clock):
    """A Redis search hit whose report fails validation is a MISS, deleted from Redis."""
    remote = ScriptedStore()
    cache = _cache(remote, clock)
    await cache.initialize()
    vector = np.array(cache.embedding_provider.embed_query(QUERY), dtype=np.float32)
    corrupt = VectorEntry(
        doc_id="corrupt", query=QUERY, service_scope="", report_json="{not json", vector=vector
    )
    await remote.put(corrupt, ttl=60)

    assert await cache.lookup(QUERY) is None
    assert remote.count == 0
    assert cache.driver == "redisearch"


@pytest.mark.parametrize(
    ("searchable", "driver"), [(True, "redisearch"), (False, "redis_hash_fallback")]
)
async def test_clear_while_down_clears_redis_on_reconnect_before_serving(clock, searchable, driver):
    """A clear while Redis is down is replayed on reconnect, before any search or hydration."""
    remote = ScriptedStore(searchable=searchable)
    cache = _cache(remote, clock)
    await cache.initialize()
    await cache.store(QUERY, _report())
    remote.down = True
    await cache.check_connection()  # drops Redis
    assert cache.driver == "in_memory"

    await cache.clear()  # what POST /cache/clear calls while Redis is down
    remote.down = False
    clock.now += cache.reconnect_cooldown_seconds + 1

    match = await cache.lookup(QUERY)  # reconnects

    assert match is None
    assert cache.driver == driver
    assert remote.count == 0
    assert cache.count == 0
