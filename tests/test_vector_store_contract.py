"""Adapter contract suite: every VectorStore adapter must pass these, parametrised by adapter."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import numpy as np
import pytest
import pytest_asyncio

from resilient_triage.cache.in_memory_store import InMemoryVectorStore
from resilient_triage.cache.vector_store import VectorEntry, VectorStore

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


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest_asyncio.fixture(params=["in_memory"])
async def make_harness(request: pytest.FixtureRequest) -> AsyncIterator[HarnessFactory]:
    """Build harnesses for the adapter named by the param; #54 adds the Redis case."""
    built: list[Harness] = []

    async def factory(max_capacity: int = 100) -> Harness:
        harness = _in_memory_harness(max_capacity)
        await harness.store.clear()
        built.append(harness)
        return harness

    yield factory
    for harness in built:
        await harness.store.clear()


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
