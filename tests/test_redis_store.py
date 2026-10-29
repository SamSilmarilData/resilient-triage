"""RedisVectorStore tests against a mocked redis client: failure kinds, modes, SCAN-based clear."""

import numpy as np
import pytest
import redis.exceptions

from resilient_triage.cache.redis_store import RedisVectorStore
from resilient_triage.cache.vector_store import StoreQueryError, StoreUnavailable, VectorEntry


def _client(mocker, *, search: bool = True):
    """A mocked async redis client that answers PING and reports the search module or not."""
    client = mocker.AsyncMock()
    client.module_list.return_value = [{b"name": b"search"}] if search else []
    if not search:
        client.execute_command.side_effect = redis.exceptions.ResponseError("unknown command")
    ft = mocker.MagicMock()
    ft.info = mocker.AsyncMock(return_value={})
    ft.search = mocker.AsyncMock()
    client.ft = mocker.MagicMock(return_value=ft)
    return client


async def _connected(client, dimension: int = 4) -> RedisVectorStore:
    store = RedisVectorStore("redis://127.0.0.1:6379/0", dimension, client_factory=lambda _: client)
    await store.connect()
    return store


def _entry() -> VectorEntry:
    return VectorEntry(
        doc_id="d1",
        query="q",
        service_scope="",
        report_json="{}",
        vector=np.ones(4, dtype=np.float32),
    )


async def test_scan_iter_key_enumeration(mocker):
    """clear() enumerates keys with a SCAN cursor and deletes them in one batch, never KEYS."""
    client = _client(mocker, search=False)

    async def scan_iter(match=None, count=None):
        assert match == "triage:doc:*"
        for k in [b"triage:doc:1", b"triage:doc:2"]:
            yield k

    client.scan_iter = scan_iter
    store = await _connected(client)

    await store.clear()

    client.delete.assert_awaited_once_with(b"triage:doc:1", b"triage:doc:2")
    client.keys.assert_not_called()


async def test_without_search_module_search_is_unsupported(mocker):
    """Hash mode: supports_search is False and search reports a query error, not a hit."""
    store = await _connected(_client(mocker, search=False))

    assert store.supports_search is False
    with pytest.raises(StoreQueryError, match="unsupported"):
        await store.search(np.ones(4, dtype=np.float32), scope="", threshold=0.9)


async def test_connect_to_unreachable_redis_raises_store_unavailable(mocker):
    """A refused connection is StoreUnavailable, so the manager fails over."""
    client = _client(mocker)
    client.ping.side_effect = redis.exceptions.ConnectionError("Connection refused")
    store = RedisVectorStore("redis://x", 4, client_factory=lambda _: client)

    with pytest.raises(StoreUnavailable):
        await store.connect()


@pytest.mark.parametrize(
    "error",
    [
        redis.exceptions.ConnectionError("reset"),
        redis.exceptions.TimeoutError("timed out"),
        ConnectionResetError("reset by peer"),
        TimeoutError("timed out"),
        OSError("network unreachable"),
    ],
)
async def test_connection_class_errors_raise_store_unavailable(mocker, error):
    """Every connection-class failure from search, put or ping is StoreUnavailable."""
    client = _client(mocker)
    store = await _connected(client)
    client.ft.return_value.search.side_effect = error
    client.hset.side_effect = error
    client.ping.side_effect = error

    with pytest.raises(StoreUnavailable):
        await store.search(np.ones(4, dtype=np.float32), scope="", threshold=0.9)
    with pytest.raises(StoreUnavailable):
        await store.put(_entry(), ttl=60)
    with pytest.raises(StoreUnavailable):
        await store.ping()


async def test_query_errors_raise_store_query_error(mocker):
    """A reachable Redis that rejects a command (syntax error, wrong type) is StoreQueryError."""
    client = _client(mocker)
    store = await _connected(client)
    client.ft.return_value.search.side_effect = redis.exceptions.ResponseError("Syntax error")
    client.hset.side_effect = redis.exceptions.ResponseError("WRONGTYPE")

    with pytest.raises(StoreQueryError):
        await store.search(np.ones(4, dtype=np.float32), scope="billing", threshold=0.9)
    with pytest.raises(StoreQueryError):
        await store.put(_entry(), ttl=60)


async def test_aclose_releases_the_client_pool(mocker):
    """aclose() closes the client, and a closed store is unavailable rather than broken."""
    client = _client(mocker)
    store = await _connected(client)

    await store.aclose()

    client.aclose.assert_awaited_once()
    with pytest.raises(StoreUnavailable):
        await store.ping()


async def test_entries_skip_hashes_missing_a_vector_or_report(mocker):
    """Hydration reads yield whole entries with their TTL and skip incomplete hashes."""
    client = _client(mocker, search=False)
    vector = np.arange(4, dtype=np.float32)
    hashes = {
        b"triage:doc:good": {
            b"query": b"q",
            b"service_scope": b"actions",
            b"report": b"{}",
            b"vector": vector.tobytes(),
        },
        b"triage:doc:no-vector": {b"query": b"q", b"report": b"{}"},
    }

    async def scan_iter(match=None, count=None):
        for k in hashes:
            yield k

    client.scan_iter = scan_iter
    client.hgetall.side_effect = lambda key: hashes[key]
    client.ttl.return_value = 42
    store = await _connected(client)

    entries = [e async for e in store.entries()]

    assert len(entries) == 1
    entry, ttl = entries[0]
    assert entry.doc_id == "good"
    assert entry.service_scope == "actions"
    assert np.array_equal(entry.vector, vector)
    assert ttl == 42
