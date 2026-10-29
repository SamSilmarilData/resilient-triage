"""RedisVectorStore: the Redis adapter behind the VectorStore seam (RediSearch or plain hashes)."""

import logging
import math
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import numpy as np
import redis.asyncio as aioredis
import redis.exceptions

from resilient_triage.cache.vector_store import (
    StoreQueryError,
    StoreUnavailable,
    VectorEntry,
    VectorHit,
)

logger = logging.getLogger(__name__)

# Frozen contract: the index name, key prefix, hash fields and index schema below.
INDEX_NAME = "triage_vector_idx"
DOC_PREFIX = "triage:doc:"
_DELETE_BATCH = 500

# Connection-class failures: the store is unreachable, so the manager fails over.
_UNAVAILABLE: tuple[type[BaseException], ...] = (
    redis.exceptions.ConnectionError,
    redis.exceptions.TimeoutError,
    ConnectionError,
    TimeoutError,
    OSError,
)

ClientFactory = Callable[[str], aioredis.Redis]


def _new_client(url: str) -> aioredis.Redis:
    """Build a Redis client with short socket timeouts, so a dead Redis never stalls a request."""
    return aioredis.from_url(
        url, decode_responses=False, socket_connect_timeout=1.0, socket_timeout=1.0
    )


@contextmanager
def _store_errors(operation: str) -> Iterator[None]:
    """Translate Redis failures into StoreUnavailable (connection-class) or StoreQueryError."""
    try:
        yield
    except (StoreUnavailable, StoreQueryError):
        raise
    except _UNAVAILABLE as err:
        raise StoreUnavailable(f"Redis {operation} failed: {err}") from err
    except Exception as err:
        raise StoreQueryError(f"Redis {operation} failed: {err}") from err


def _to_str(val: Any) -> str:
    """Decode a Redis bytes or str value to str."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _field(data: dict[Any, Any], name: str) -> Any:
    """Read a hash field whether the client returned bytes or str keys."""
    return data.get(name.encode(), data.get(name))


class RedisVectorStore:
    """VectorStore over Redis hashes `triage:doc:<id>`, searched by the RediSearch HNSW index.

    Without the search module, `supports_search` is False and `search` raises StoreQueryError
    (the `redis_hash_fallback` mode); the manager then serves reads from its standby and
    hydrates it from `entries()`.
    """

    def __init__(
        self, url: str, dimension: int, client_factory: ClientFactory = _new_client
    ) -> None:
        self.url = url
        self.dimension = dimension
        self._client_factory = client_factory
        self._client: aioredis.Redis | None = None
        self._supports_search = False
        # Doc ids this adapter has written or read; expiries are not tracked, as in memory.
        self._known: set[str] = set()

    @property
    def supports_search(self) -> bool:
        """Whether the RediSearch vector index is available for `search`."""
        return self._supports_search

    @property
    def count(self) -> int:
        """Number of entries this adapter has put or enumerated since its last clear."""
        return len(self._known)

    async def connect(self) -> None:
        """Connect, detect the search module and ensure the index; raises StoreUnavailable."""
        client = self._client_factory(self.url)
        self._client = client
        with _store_errors("connect"):
            await client.ping()
        self._supports_search = await self._detect_search(client)
        if self._supports_search:
            self._supports_search = await self._ensure_index(client)

    async def ping(self) -> None:
        """Round-trip a PING; raises StoreUnavailable when Redis does not answer."""
        with _store_errors("ping"):
            await self._require_client().ping()

    async def aclose(self) -> None:
        """Release the client's connection pool; never raises."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.aclose()
        except Exception as err:
            logger.debug("Error closing Redis connection: %s", err)

    async def search(self, vector: np.ndarray, scope: str, threshold: float) -> VectorHit | None:
        """Return the KNN-nearest entry in `scope` if its similarity is at or above `threshold`."""
        if not self._supports_search:
            raise StoreQueryError("Redis search is unsupported without the search module")
        from redis.commands.search.query import Query

        # Scopes are normalize_scope()'d, so only "-" needs escaping for the tag grammar.
        if scope:
            safe_scope = scope.replace("-", "\\-")
            filter_query = f"(@service_scope:{{{safe_scope}}})=>[KNN 1 @vector $vec AS score]"
        else:
            filter_query = "*=>[KNN 1 @vector $vec AS score]"
        q = (
            Query(filter_query)
            .sort_by("score")
            .return_fields("query", "service_scope", "report", "score")
            .paging(0, 1)
            .dialect(2)
        )
        query_bytes = np.asarray(vector, dtype=np.float32).tobytes()
        with _store_errors("search"):
            res = (
                await self._require_client()
                .ft(INDEX_NAME)
                .search(q, query_params={"vec": query_bytes})
            )
            if not res.docs:
                return None
            doc = res.docs[0]
            # The index metric is COSINE, whose score is a distance: 1 - similarity.
            similarity = 1.0 - float(getattr(doc, "score", 1.0))
            if similarity < threshold:
                return None
            return VectorHit(
                doc_id=_to_str(doc.id).removeprefix(DOC_PREFIX),
                query=_to_str(getattr(doc, "query", "")),
                service_scope=_to_str(getattr(doc, "service_scope", "")),
                report_json=_to_str(doc.report),
                similarity=similarity,
            )

    async def put(self, entry: VectorEntry, ttl: float) -> None:
        """Write the entry's hash and expire it after `ttl` (rounded up to whole seconds)."""
        mapping: dict[str | bytes, bytes | str] = {
            "query": entry.query,
            "service_scope": entry.service_scope,
            "report": entry.report_json,
            "vector": np.asarray(entry.vector, dtype=np.float32).tobytes(),
        }
        key = f"{DOC_PREFIX}{entry.doc_id}"
        with _store_errors("put"):
            client = self._require_client()
            await client.hset(key, mapping=mapping)  # type: ignore[arg-type]
            await client.expire(key, max(1, math.ceil(ttl)))
        self._known.add(entry.doc_id)

    async def clear(self) -> None:
        """Delete every `triage:doc:*` key with SCAN + batched DEL, so Redis never blocks."""
        with _store_errors("clear"):
            client = self._require_client()
            keys = [k async for k in client.scan_iter(match=f"{DOC_PREFIX}*", count=100)]
            for i in range(0, len(keys), _DELETE_BATCH):
                await client.delete(*keys[i : i + _DELETE_BATCH])
        self._known.clear()

    async def entries(self) -> AsyncIterator[tuple[VectorEntry, float]]:
        """Yield every stored entry with its remaining TTL in seconds, for standby hydration.

        A hash missing its vector or report is skipped. The vector is decoded but not
        validated here; the standby's `put` is where an inbound vector is checked.
        """
        with _store_errors("scan"):
            client = self._require_client()
            keys = [k async for k in client.scan_iter(match=f"{DOC_PREFIX}*", count=100)]
        for key in keys:
            with _store_errors("hydrate"):
                data = await client.hgetall(key)
                ttl = await client.ttl(key)
            entry = self._entry_from_hash(_to_str(key), data)
            if entry is None:
                continue
            self._known.add(entry.doc_id)
            yield entry, float(ttl)

    @staticmethod
    def _entry_from_hash(key: str, data: dict[Any, Any]) -> VectorEntry | None:
        """Decode one `triage:doc:*` hash into an entry, or None if a required field is missing."""
        vec_raw = _field(data, "vector")
        report_raw = _field(data, "report")
        if not data or vec_raw is None or report_raw is None:
            return None
        vec_bytes = vec_raw if isinstance(vec_raw, bytes) else str(vec_raw).encode("utf-8")
        if len(vec_bytes) % 4:
            logger.debug("Skipping %s: vector is not a float32 buffer", key)
            return None
        return VectorEntry(
            doc_id=key.removeprefix(DOC_PREFIX),
            query=_to_str(_field(data, "query")),
            service_scope=_to_str(_field(data, "service_scope")),
            report_json=_to_str(report_raw),
            vector=np.frombuffer(vec_bytes, dtype=np.float32),
        )

    def _require_client(self) -> aioredis.Redis:
        """Return the live client, or raise StoreUnavailable if not connected."""
        if self._client is None:
            raise StoreUnavailable("Redis is not connected")
        return self._client

    async def _detect_search(self, client: aioredis.Redis) -> bool:
        """Whether the server has the search module (MODULE LIST, else a native FT._LIST)."""
        try:
            modules = await client.module_list()
            names = [_to_str(_field(m, "name")).lower() for m in modules]
            if "search" in names or "ft" in names:
                return True
        except _UNAVAILABLE as err:
            raise StoreUnavailable(f"Redis module check failed: {err}") from err
        except Exception as err:
            logger.debug("RediSearch module check: %s", err)
        try:
            await client.execute_command("FT._LIST")
            return True
        except _UNAVAILABLE as err:
            raise StoreUnavailable(f"Redis FT._LIST check failed: {err}") from err
        except Exception as err:
            logger.debug("Native FT._LIST check: %s", err)
            return False

    async def _ensure_index(self, client: aioredis.Redis) -> bool:
        """Create the HNSW index if missing; False (hash mode) if it cannot be created."""
        from redis.commands.search.field import TagField, TextField, VectorField
        from redis.commands.search.index_definition import IndexDefinition, IndexType

        try:
            await client.ft(INDEX_NAME).info()
            logger.debug("RediSearch index '%s' already exists.", INDEX_NAME)
            return True
        except _UNAVAILABLE as err:
            raise StoreUnavailable(f"Redis index check failed: {err}") from err
        except Exception:
            pass
        schema = [
            TextField("query"),
            TagField("service_scope"),
            VectorField(
                "vector",
                "HNSW",
                {"TYPE": "FLOAT32", "DIM": self.dimension, "DISTANCE_METRIC": "COSINE"},
            ),
        ]
        definition = IndexDefinition(prefix=[DOC_PREFIX], index_type=IndexType.HASH)
        try:
            await client.ft(INDEX_NAME).create_index(schema, definition=definition)
            logger.info("Created RediSearch vector index '%s'.", INDEX_NAME)
            return True
        except _UNAVAILABLE as err:
            raise StoreUnavailable(f"Redis index creation failed: {err}") from err
        except Exception as err:
            logger.warning("Failed to create RediSearch index: %s. Using hash mode.", err)
            return False
