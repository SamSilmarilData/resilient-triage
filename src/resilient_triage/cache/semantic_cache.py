"""Redis semantic caching with sub-20ms cosine similarity lookup and dual-engine fallback."""

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Callable

import numpy as np
from pydantic import BaseModel, Field

from resilient_triage.cache.embeddings import (
    BaseEmbeddingProvider,
    get_embedding_provider,
)
from resilient_triage.cache.in_memory_store import InMemoryVectorStore
from resilient_triage.cache.redis_store import DOC_PREFIX, INDEX_NAME, RedisVectorStore
from resilient_triage.cache.vector_store import (
    RemoteVectorStore,
    StoreUnavailable,
    VectorEntry,
    VectorHit,
)
from resilient_triage.config import settings
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport

logger = logging.getLogger(__name__)


def normalize_scope(service_filter: str | None) -> str:
    """Return the canonical service scope: stripped, lower-cased, [a-z0-9_-] only; "" for none."""
    if not service_filter:
        return ""
    return re.sub(r"[^a-zA-Z0-9_\-]", "", service_filter.strip().lower())


def format_scoped_query(query: str, service_filter: str | None = None) -> str:
    """Prepend service scope to incident query to contextualize vector search.

    Args:
        query: Raw incident symptoms or inquiry string.
        service_filter: Optional component or service name to constrain scope.

    Returns:
        str: Scope-prefixed string (e.g. '[scope: internal] query') or original query.
    """
    clean_q = query.strip()
    scope = normalize_scope(service_filter)
    if scope:
        return f"[scope: {scope}] {clean_q}"
    return clean_q


class CacheMatch(BaseModel):
    """Output metadata and payload when a semantic cache hit occurs."""

    report: IncidentTriageReport
    similarity: float = Field(..., description="Cosine similarity score (0.0 to 1.0)")
    execution_time_ms: float = Field(..., description="Lookup turnaround time in milliseconds")
    matched_query: str = Field(..., description="Previous query that produced the cached match")
    cache_hit: bool = Field(default=True, description="Always True for CacheMatch")


class SemanticCacheManager:
    """Semantic vector cache over a Redis VectorStore with an in-memory standby.

    Every write lands in the standby and, while Redis is up, in Redis. Reads use Redis search
    when Redis is up and searchable, otherwise the standby. Every Redis drop goes through
    `_mark_down`. `lookup`, `store` and `clear` never raise: a failure is a MISS or a no-op.
    """

    INDEX_NAME = INDEX_NAME
    DOC_PREFIX = DOC_PREFIX

    def __init__(
        self,
        redis_url: str | None = None,
        similarity_threshold: float | None = None,
        default_ttl: int | None = None,
        degraded_ttl: int | None = None,
        embedding_provider: BaseEmbeddingProvider | None = None,
        max_capacity: int = 10000,
        remote_store_factory: Callable[[], RemoteVectorStore] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.redis_url = redis_url or settings.redis_url
        self.similarity_threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else settings.cache_similarity_threshold
        )
        self.default_ttl = default_ttl if default_ttl is not None else settings.cache_ttl_seconds
        self.degraded_ttl = (
            degraded_ttl if degraded_ttl is not None else settings.cache_degraded_ttl_seconds
        )
        self.embedding_provider = embedding_provider or get_embedding_provider()
        self.max_capacity = max_capacity
        self._remote_store_factory = remote_store_factory or self._new_redis_store
        self._clock = clock

        # The connected Redis store, or None while down; only _mark_down and close clear it.
        self._redis: RemoteVectorStore | None = None
        self._initialized: bool = False
        self._lock = asyncio.Lock()
        self._last_reconnect_attempt: float | None = None
        self.reconnect_cooldown_seconds: float = 5.0
        # Set by a clear() that could not reach Redis; the next connect clears Redis first.
        self._clear_pending: bool = False

        # In-memory standby: every write lands here; it serves reads whenever Redis search can't.
        # Its dimension guard keeps a wrong-size vector (from store or hydration) out of the matrix.
        self._standby = InMemoryVectorStore(
            max_capacity=max_capacity, clock=clock, dimension=self.embedding_provider.dimension
        )

    def _new_redis_store(self) -> RemoteVectorStore:
        """Build the production Redis adapter for this manager's URL and embedding size."""
        return RedisVectorStore(self.redis_url, dimension=self.embedding_provider.dimension)

    async def initialize(self) -> None:
        """Connect to Redis (else run on the standby) and hydrate the standby in hash mode."""
        if self._initialized and self._redis is not None:
            return

        async with self._lock:
            if self._initialized and self._redis is not None:
                return
            await self._connect()
            self._initialized = True

    async def _ensure_connected(self) -> None:
        """Reconnect if Redis is down and the reconnect cooldown has passed; never blocks long."""
        if self._redis is not None or self._cooling_down():
            return

        async with self._lock:
            if self._redis is not None or self._cooling_down():
                return
            await self._connect()

    def _cooling_down(self) -> bool:
        """True within `reconnect_cooldown_seconds` of the last connect attempt or drop."""
        last = self._last_reconnect_attempt
        return last is not None and self._clock() - last < self.reconnect_cooldown_seconds

    async def _connect(self) -> None:
        """Try one connection; on success adopt the store, hydrating the standby in hash mode."""
        self._last_reconnect_attempt = self._clock()
        store = self._remote_store_factory()
        try:
            await store.connect()
        except Exception as conn_err:
            logger.warning(
                "Redis unavailable (%s). SemanticCacheManager operating in in-memory vector mode.",
                conn_err,
            )
            await store.aclose()
            return

        if self._clear_pending:
            try:
                await store.clear()
            except Exception as clear_err:
                logger.warning(
                    "Pending Redis clear failed (%s). Staying on the in-memory standby.", clear_err
                )
                await store.aclose()
                return
            self._clear_pending = False
            logger.info("Ran the pending Redis clear before serving from Redis.")

        self._redis = store
        logger.info(
            "SemanticCacheManager connected to Redis at %s (RediSearch: %s)",
            self.redis_url,
            store.supports_search,
        )
        if not store.supports_search:
            await self._hydrate_from(store)

    async def _hydrate_from(self, store: RemoteVectorStore) -> None:
        """Copy Redis hashes into the standby so hash mode can serve reads from memory."""
        try:
            async for entry, ttl in store.entries():
                try:
                    await self._standby.put(entry, ttl=ttl if ttl > 0 else self.default_ttl)
                except ValueError as rejected:
                    logger.debug("Skipping hydrated document %s: %s", entry.doc_id, rejected)
        except StoreUnavailable as err:
            await self._mark_down(f"hydration failed: {err}")
            return
        except Exception as err:
            logger.warning("Error hydrating from Redis: %s", err)
        logger.info("Hydrated %d cached documents from Redis into memory.", self._standby.count)

    async def _mark_down(self, reason: str) -> None:
        """Drop Redis: release its pool with aclose() and start the reconnect cooldown."""
        store = self._redis
        if store is None:
            return
        self._redis = None
        self._last_reconnect_attempt = self._clock()
        logger.warning("Redis marked down (%s). Serving from the in-memory standby.", reason)
        await store.aclose()

    async def check_connection(self) -> bool:
        """Actively test Redis connectivity and attempt reconnection if offline.

        Returns:
            bool: True if Redis is reachable and responsive to PING; False otherwise.
        """
        if self._redis is None:
            await self._ensure_connected()
            return self._redis is not None

        try:
            await self._redis.ping()
            return True
        except Exception as ping_err:
            await self._mark_down(f"ping failed: {ping_err}")
            return False

    async def lookup(
        self,
        query: str,
        service_filter: str | None = None,
        threshold: float | None = None,
        force_refresh: bool = False,
    ) -> CacheMatch | None:
        """Perform semantic similarity search against cached triage reports.

        Args:
            query: Incident query or symptom description.
            service_filter: Optional component scope to prepend to query.
            threshold: Cosine similarity cutoff (defaults to settings.cache_similarity_threshold).
            force_refresh: If True, bypasses cache lookup entirely.

        Returns:
            CacheMatch | None: Cached report and metadata if similarity >= threshold; None on MISS.
        """
        if force_refresh:
            logger.debug("force_refresh=True: bypassing semantic cache lookup.")
            return None

        await self._ready()

        start_time = time.monotonic()
        effective_threshold = threshold if threshold is not None else self.similarity_threshold
        target_scope = normalize_scope(service_filter)
        scoped_q = format_scoped_query(query, service_filter)

        # 1. Generate query embedding; a failing embedder degrades to a MISS
        try:
            query_vec = np.array(self.embedding_provider.embed_query(scoped_q), dtype=np.float32)
        except Exception as embed_err:
            logger.warning("Embedding failed during cache lookup: %s. Treating as MISS.", embed_err)
            return None

        # 2. Search Redis if it is up and searchable; a Redis MISS still checks the standby
        redis = self._redis
        if redis is not None and redis.supports_search:
            try:
                redis_hit = await redis.search(query_vec, target_scope, effective_threshold)
            except StoreUnavailable as conn_err:
                await self._mark_down(f"search failed: {conn_err}")
            except Exception as query_err:
                logger.warning(
                    "Redis search error: %s. Falling back to the standby without disconnecting.",
                    query_err,
                )
            else:
                if redis_hit is not None:
                    match = self._to_match(redis_hit, start_time)
                    if match is None:
                        await self._evict(redis_hit.doc_id)
                    return match

        # 3. Fall back to the in-memory standby
        hit = await self._standby.search(query_vec, target_scope, effective_threshold)
        if hit is None:
            return None
        match = self._to_match(hit, start_time)
        if match is None:
            await self._standby.delete(hit.doc_id)
        return match

    async def _ready(self) -> None:
        """Initialize on first use, else attempt a cooldown-throttled reconnect while down."""
        if not self._initialized:
            await self.initialize()
        elif self._redis is None:
            await self._ensure_connected()

    async def _evict(self, doc_id: str) -> None:
        """Delete an invalid entry from the standby and, while up, from Redis; never raises."""
        await self._standby.delete(doc_id)
        redis = self._redis
        if redis is None:
            return
        try:
            await redis.delete(doc_id)
        except StoreUnavailable as conn_err:
            await self._mark_down(f"delete failed: {conn_err}")
        except Exception as err:
            logger.warning("Redis delete of invalid entry %s failed: %s", doc_id, err)

    @staticmethod
    def _to_match(hit: VectorHit, start_time: float) -> CacheMatch | None:
        """Validate a store hit's report and wrap it as a CacheMatch; None if it is invalid."""
        try:
            report = IncidentTriageReport.model_validate(json.loads(hit.report_json))
        except ValueError as invalid:  # JSONDecodeError and ValidationError both subclass it
            logger.warning(
                "Cached report %s failed validation: %s. Treating as MISS and evicting.",
                hit.doc_id,
                invalid,
            )
            return None
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        return CacheMatch(
            report=report,
            similarity=round(hit.similarity, 4),
            execution_time_ms=round(elapsed_ms, 2),
            matched_query=hit.query,
            cache_hit=True,
        )

    async def store(
        self,
        query: str,
        report: IncidentTriageReport,
        service_filter: str | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store triage report and its vector embedding in memory and Redis.

        Automatically selects TTL: healthy reports receive settings.cache_ttl_seconds (300s),
        while degraded reports receive settings.cache_degraded_ttl_seconds (60s) to allow
        rapid re-evaluation once upstream systems recover.

        Args:
            query: Raw incident query string.
            report: Validated IncidentTriageReport to persist.
            service_filter: Optional component or service filter string.
            ttl_seconds: Optional explicit TTL override in seconds.
        """
        await self._ready()

        target_scope = normalize_scope(service_filter)
        scoped_q = format_scoped_query(query, service_filter)
        try:
            query_vec = self.embedding_provider.embed_query(scoped_q)
        except Exception as embed_err:
            logger.warning("Embedding failed during cache store: %s. Skipping store.", embed_err)
            return

        # Degraded reports use shorter 60s TTL to allow quick recovery re-evaluation
        if ttl_seconds is not None:
            effective_ttl = ttl_seconds
        elif (
            report.degradation_status != DegradationStatus.HEALTHY
            or len(report.circuit_breakers_tripped) > 0
        ):
            effective_ttl = self.degraded_ttl
        else:
            effective_ttl = self.default_ttl

        doc_id = str(uuid.uuid4())
        report_json = report.model_dump_json()
        entry = VectorEntry(
            doc_id=doc_id,
            query=scoped_q,
            service_scope=target_scope,
            report_json=report_json,
            vector=np.array(query_vec, dtype=np.float32),
        )
        # 1. Write through to the in-memory standby (it evicts oldest-first at capacity)
        try:
            await self._standby.put(entry, ttl=effective_ttl)
        except ValueError as rejected:
            logger.warning("Cache store rejected the embedding: %s. Skipping store.", rejected)
            return

        # 2. Write to Redis too while it is up; only a connection failure drops it
        redis = self._redis
        if redis is None:
            return
        try:
            await redis.put(entry, ttl=effective_ttl)
        except StoreUnavailable as conn_err:
            await self._mark_down(f"store failed: {conn_err}")
        except Exception as err:
            logger.warning("Redis store error: %s (in-memory copy active).", err)

    async def clear(self) -> None:
        """Clear all in-memory and Redis cache entries.

        While Redis is down, this reconnects at once (ignoring the cooldown); if Redis is still
        down, the clear stays pending and runs on the next successful connect, before Redis
        serves a search or hydrates the standby.
        """
        await self._standby.clear()
        redis = self._redis
        if redis is None:
            self._clear_pending = True
            async with self._lock:
                if self._redis is None:
                    await self._connect()
            return
        try:
            await redis.clear()
        except StoreUnavailable as conn_err:
            await self._mark_down(f"clear failed: {conn_err}")
        except Exception as err:
            logger.warning("Redis clear error: %s", err)

    @property
    def driver(self) -> str:
        """Return the active vector search driver name."""
        if self._redis is not None:
            if self._redis.supports_search:
                return "redisearch"
            return "redis_hash_fallback"
        return "in_memory"

    @property
    def count(self) -> int:
        """Return total count of cached documents in memory."""
        return self._standby.count

    async def close(self) -> None:
        """Gracefully close Redis client connection and reset state."""
        store = self._redis
        self._redis = None
        self._initialized = False
        if store is not None:
            await store.aclose()
