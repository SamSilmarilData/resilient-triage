"""Adapter contract suite: every VectorStore adapter must pass these, parametrised by adapter."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import numpy as np
import pytest
import pytest_asyncio

from resilient_triage.cache.in_memory_store import InMemoryVectorStore
from resilient_triage.cache.redis_store import RedisVectorStore
from resilient_triage.cache.vector_store import StoreUnavailable, VectorEntry, VectorStore
from resilient_triage.config import settings

DIM = 384  # The live index dimension, so a Redis adapter can run the same vectors.


def _unit(i: int) -> np.ndarray:
    vec = np.zeros(DIM, dtype=np.float32)
    vec[i] = 1.0
    return vec


# Cosine similarity of NEAR to E0 is exactly 0.8 (0.8 * 1 + 0.6 * 0).
E0 = _unit(0)
NEAR = (0.8 * _unit(0) + 0.6 * _unit(1)).astype(np.float32)
FAR = _unit(2)


def _entry(doc_id: str, vector: np.ndarray, scope: str = "") -> VectorEntry:
    return VectorEntry(
        doc_id=doc_id,
        query=f"query {doc_id}",
        service_scope=scope,
        report_json=f'{{"summary": "{doc_id}"}}',
        vector=vector,
    )


@dataclass
class Harness:
    """One adapter under test, plus a way to move its clock past a TTL."""

    store: VectorStore
    advance: Callable[[float], Awaitable[None]]
    bounded: bool = True  # Whether the adapter enforces max_capacity itself.


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _in_memory_harness(max_capacity: int) -> Harness:
    clock = _FakeClock()

    async def advance(seconds: float) -> None:
        clock.now += seconds

    return Harness(InMemoryVectorStore(max_capacity=max_capacity, clock=clock), advance)


async def _redis_harness(max_capacity: int) -> Harness:
    """A RedisVectorStore on the configured Redis; skips the test when it is unreachable.

    Redis expires keys in whole seconds, so `advance` really sleeps. Redis does not bound
    capacity (keys expire by TTL instead), so the harness is unbounded.
    """
    store = RedisVectorStore(settings.redis_url, dimension=DIM)
    try:
        await store.connect()
    except StoreUnavailable as err:
        await store.aclose()
        pytest.skip(f"Local Redis is not reachable at {settings.redis_url}: {err}")
    if not store.supports_search:
        await store.aclose()
        pytest.skip("Local Redis has no search module; the Redis contract needs RediSearch")

    async def advance(seconds: float) -> None:
        await asyncio.sleep(seconds)

    return Harness(store, advance, bounded=False)


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest_asyncio.fixture(params=["in_memory", "redis"])
async def make_harness(request: pytest.FixtureRequest) -> AsyncIterator[HarnessFactory]:
    """Build harnesses for the adapter named by the param, each starting empty."""
    built: list[Harness] = []

    async def factory(max_capacity: int = 100) -> Harness:
        if request.param == "redis":
            harness = await _redis_harness(max_capacity)
        else:
            harness = _in_memory_harness(max_capacity)
        built.append(harness)
        await harness.store.clear()
        return harness

    yield factory
    for harness in built:
        await harness.store.clear()
        if isinstance(harness.store, RedisVectorStore):
            await harness.store.aclose()


async def test_search_returns_entry_at_or_above_threshold(make_harness: HarnessFactory) -> None:
    """A stored vector within the threshold comes back with its similarity and payload."""
    store = (await make_harness()).store
    await store.put(_entry("a", E0), ttl=60)

    hit = await store.search(NEAR, scope="", threshold=0.75)

    assert hit is not None
    assert hit.doc_id == "a"
    assert hit.query == "query a"
    assert hit.report_json == '{"summary": "a"}'
    assert hit.similarity == pytest.approx(0.8, abs=1e-4)


async def test_search_misses_below_threshold(make_harness: HarnessFactory) -> None:
    """A best match under the threshold is a MISS."""
    store = (await make_harness()).store
    await store.put(_entry("a", E0), ttl=60)

    assert await store.search(NEAR, scope="", threshold=0.9) is None


async def test_search_returns_the_most_similar_entry(make_harness: HarnessFactory) -> None:
    """With several candidates over the threshold, the closest one wins."""
    store = (await make_harness()).store
    await store.put(_entry("near", NEAR), ttl=60)
    await store.put(_entry("exact", E0), ttl=60)
    await store.put(_entry("far", FAR), ttl=60)

    hit = await store.search(E0, scope="", threshold=0.5)

    assert hit is not None
    assert hit.doc_id == "exact"
    assert hit.similarity == pytest.approx(1.0, abs=1e-4)


async def test_search_on_empty_store_misses(make_harness: HarnessFactory) -> None:
    """An empty store is a MISS, not an error."""
    store = (await make_harness()).store

    assert await store.search(E0, scope="", threshold=0.0) is None


async def test_scoped_search_only_sees_entries_in_that_scope(make_harness: HarnessFactory) -> None:
    """An identical vector under another scope is invisible to a scoped search."""
    store = (await make_harness()).store
    await store.put(_entry("actions-doc", E0, scope="actions"), ttl=60)
    await store.put(_entry("billing-doc", E0, scope="billing"), ttl=60)

    actions = await store.search(E0, scope="actions", threshold=0.9)
    billing = await store.search(E0, scope="billing", threshold=0.9)

    assert actions is not None and actions.doc_id == "actions-doc"
    assert actions.service_scope == "actions"
    assert billing is not None and billing.doc_id == "billing-doc"
    assert await store.search(E0, scope="database", threshold=0.9) is None


async def test_unscoped_search_never_returns_a_scoped_entry(make_harness: HarnessFactory) -> None:
    """ "No scope" is a scope of its own: an unscoped search skips scoped entries."""
    store = (await make_harness()).store
    await store.put(_entry("billing-doc", E0, scope="billing"), ttl=60)

    assert await store.search(E0, scope="", threshold=0.0) is None


async def test_scoped_search_never_returns_an_unscoped_entry(make_harness: HarnessFactory) -> None:
    """A scoped search skips entries stored with no scope."""
    store = (await make_harness()).store
    await store.put(_entry("unscoped-doc", E0), ttl=60)

    assert await store.search(E0, scope="billing", threshold=0.0) is None


async def test_exact_scope_match_hits_past_closer_entries_in_other_scopes(
    make_harness: HarnessFactory,
) -> None:
    """The best entry in the exact scope wins even when closer entries sit in other scopes."""
    store = (await make_harness()).store
    await store.put(_entry("billing-exact", E0, scope="billing"), ttl=60)
    await store.put(_entry("actions-exact", E0, scope="actions"), ttl=60)
    await store.put(_entry("unscoped-near", NEAR), ttl=60)

    unscoped = await store.search(E0, scope="", threshold=0.5)
    billing = await store.search(E0, scope="billing", threshold=0.5)

    assert unscoped is not None and unscoped.doc_id == "unscoped-near"
    assert unscoped.service_scope == ""
    assert unscoped.similarity == pytest.approx(0.8, abs=1e-4)
    assert billing is not None and billing.doc_id == "billing-exact"
    assert billing.similarity == pytest.approx(1.0, abs=1e-4)


async def test_entry_expires_after_its_ttl(make_harness: HarnessFactory) -> None:
    """An entry is served until its TTL passes, then it is a MISS."""
    harness = await make_harness()
    await harness.store.put(_entry("short", E0), ttl=1)
    await harness.store.put(_entry("long", FAR), ttl=60)

    assert await harness.store.search(E0, scope="", threshold=0.9) is not None

    await harness.advance(2)

    assert await harness.store.search(E0, scope="", threshold=0.9) is None
    long_hit = await harness.store.search(FAR, scope="", threshold=0.9)
    assert long_hit is not None and long_hit.doc_id == "long"


async def test_capacity_evicts_the_oldest_entry(make_harness: HarnessFactory) -> None:
    """Putting past capacity drops the oldest entry and keeps the newest."""
    harness = await make_harness(max_capacity=2)
    if not harness.bounded:
        pytest.skip("this adapter does not bound capacity; Redis entries expire by TTL")
    store = harness.store
    await store.put(_entry("first", E0), ttl=60)
    await store.put(_entry("second", FAR), ttl=60)
    await store.put(_entry("third", _unit(3)), ttl=60)

    assert store.count == 2
    assert await store.search(E0, scope="", threshold=0.9) is None
    third = await store.search(_unit(3), scope="", threshold=0.9)
    assert third is not None and third.doc_id == "third"


async def test_put_with_existing_doc_id_replaces_without_evicting(
    make_harness: HarnessFactory,
) -> None:
    """Re-putting a doc id (as hydration does) overwrites it rather than evicting another."""
    store = (await make_harness(max_capacity=2)).store
    await store.put(_entry("first", E0), ttl=60)
    await store.put(_entry("second", FAR), ttl=60)
    await store.put(_entry("second", FAR), ttl=60)

    assert store.count == 2
    assert await store.search(E0, scope="", threshold=0.9) is not None


async def test_clear_removes_every_entry(make_harness: HarnessFactory) -> None:
    """After clear, nothing is counted or found."""
    store = (await make_harness()).store
    await store.put(_entry("a", E0), ttl=60)
    await store.put(_entry("b", FAR, scope="actions"), ttl=60)

    await store.clear()

    assert store.count == 0
    assert await store.search(E0, scope="", threshold=0.0) is None
    assert await store.search(FAR, scope="actions", threshold=0.0) is None


async def test_count_tracks_puts(make_harness: HarnessFactory) -> None:
    """Count is the number of entries put."""
    store = (await make_harness()).store
    assert store.count == 0

    await store.put(_entry("a", E0), ttl=60)
    await store.put(_entry("b", FAR, scope="actions"), ttl=60)

    assert store.count == 2


async def test_delete_removes_only_that_entry(make_harness: HarnessFactory) -> None:
    """Deleting a doc id drops that entry, keeps the rest, and is a no-op for an unknown id."""
    store = (await make_harness()).store
    await store.put(_entry("gone", E0), ttl=60)
    await store.put(_entry("kept", FAR), ttl=60)

    await store.delete("gone")
    await store.delete("never-stored")

    assert store.count == 1
    assert await store.search(E0, scope="", threshold=0.9) is None
    kept = await store.search(FAR, scope="", threshold=0.9)
    assert kept is not None and kept.doc_id == "kept"
