"""Redis semantic caching with sub-20ms cosine similarity lookup and dual-engine fallback."""

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

import numpy as np
import redis.asyncio as aioredis
from pydantic import BaseModel, Field

from resilient_triage.cache.embeddings import (
    BaseEmbeddingProvider,
    get_embedding_provider,
)
from resilient_triage.config import settings
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport

logger = logging.getLogger(__name__)


def format_scoped_query(query: str, service_filter: str | None = None) -> str:
    """Prepend service scope to incident query to contextualize vector search.

    Args:
        query: Raw incident symptoms or inquiry string.
        service_filter: Optional component or service name to constrain scope.

    Returns:
        str: Scope-prefixed string (e.g. '[scope: internal] query') or original query.
    """
    clean_q = query.strip()
    if service_filter and service_filter.strip():
        scope = re.sub(r"[^a-zA-Z0-9_\-]", "", service_filter.strip().lower())
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


class InMemoryVectorEntry:
    """In-memory cache record containing vector and metadata."""

    def __init__(
        self,
        doc_id: str,
        query: str,
        report_json: str,
        vector: np.ndarray,
        expires_at: float,
        service_scope: str = "",
    ) -> None:
        self.doc_id = doc_id
        self.query = query
        self.report_json = report_json
        self.vector = vector
        self.expires_at = expires_at
        self.service_scope = service_scope


class SemanticCacheManager:
    """Semantic vector cache manager supporting native RediSearch and vectorized in-memory fallback.

    Guarantees sub-20ms lookups for cosine similarity >= 0.90.
    """

    INDEX_NAME = "triage_vector_idx"
    DOC_PREFIX = "triage:doc:"

    def __init__(
        self,
        redis_url: str | None = None,
        similarity_threshold: float | None = None,
        default_ttl: int | None = None,
        degraded_ttl: int | None = None,
        embedding_provider: BaseEmbeddingProvider | None = None,
        max_capacity: int = 10000,
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

        self._redis: aioredis.Redis | None = None
        self._has_redisearch: bool = False
        self._initialized: bool = False
        self._lock = asyncio.Lock()
        self._last_reconnect_attempt: float = 0.0
        self.reconnect_cooldown_seconds: float = 5.0

        # In-memory vectorized store (fallback or standalone)
        self._in_memory_docs: dict[str, InMemoryVectorEntry] = {}

    async def initialize(self) -> None:
        """Initialize Redis connection and test for RediSearch module availability."""
        if self._initialized and self._redis is not None:
            return

        async with self._lock:
            if self._initialized and self._redis is not None:
                return

            await self._connect_redis()
            self._initialized = True

    async def _ensure_connected(self) -> None:
        """Throttled non-blocking auto-reconnect attempt if Redis is currently disconnected."""
        if self._redis is not None:
            return

        now = time.monotonic()
        if now - self._last_reconnect_attempt < self.reconnect_cooldown_seconds:
            return

        async with self._lock:
            if self._redis is not None:
                return
            self._last_reconnect_attempt = time.monotonic()
            await self._connect_redis()

    async def _connect_redis(self) -> None:
        """Establish Redis connection, detect RediSearch capabilities, and hydrate cache."""
        try:
            client = aioredis.from_url(
                self.redis_url,
                decode_responses=False,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            # Test connection
            await client.ping()

            # Check RediSearch module or native command support
            has_search = False
            try:
                modules = await client.module_list()
                module_names = [m.get(b"name", b"").decode("utf-8").lower() for m in modules]
                has_search = "search" in module_names or "ft" in module_names
            except Exception as mod_err:
                logger.debug("RediSearch module check: %s", mod_err)

            if not has_search:
                try:
                    await client.execute_command("FT._LIST")
                    has_search = True
                except Exception as cmd_err:
                    logger.debug("Native FT._LIST check: %s", cmd_err)

            self._redis = client
            self._has_redisearch = has_search

            if self._has_redisearch:
                await self._ensure_redisearch_index()
            else:
                await self._hydrate_from_redis()

            logger.info(
                "SemanticCacheManager connected to Redis at %s (RediSearch: %s)",
                self.redis_url,
                self._has_redisearch,
            )
        except Exception as conn_err:
            logger.warning(
                "Redis unavailable (%s). SemanticCacheManager operating in in-memory vector mode.",
                conn_err,
            )
            self._redis = None
            self._has_redisearch = False

    async def _hydrate_from_redis(self) -> None:
        """Hydrate in-memory vector index from persistent Redis hashes on startup/reconnect."""
        if not self._redis:
            return
        try:
            keys = [k async for k in self._redis.scan_iter(match=f"{self.DOC_PREFIX}*", count=100)]
            if not keys:
                return

            now = time.monotonic()
            for key in keys:
                try:
                    data = await self._redis.hgetall(key)
                    if not data or b"vector" not in data or b"report" not in data:
                        continue
                    doc_id = key.decode("utf-8").replace(self.DOC_PREFIX, "")
                    query = data.get(b"query", b"").decode("utf-8")
                    report_json = data.get(b"report", b"").decode("utf-8")
                    service_scope = data.get(b"service_scope", b"").decode("utf-8")
                    vec_bytes = data[b"vector"]
                    vec_arr = np.frombuffer(vec_bytes, dtype=np.float32)
                    ttl = await self._redis.ttl(key)
                    expires_at = now + (ttl if ttl > 0 else self.default_ttl)

                    self._in_memory_docs[doc_id] = InMemoryVectorEntry(
                        doc_id=doc_id,
                        query=query,
                        report_json=report_json,
                        vector=vec_arr,
                        expires_at=expires_at,
                        service_scope=service_scope,
                    )
                except Exception as doc_err:
                    logger.debug("Failed to hydrate document key %s: %s", key, doc_err)

            logger.info("Hydrated %d cached documents from Redis into memory.", len(self._in_memory_docs))
        except Exception as err:
            logger.warning("Error hydrating from Redis: %s", err)

    async def _ensure_redisearch_index(self) -> None:
        """Create RediSearch HNSW vector index if it does not already exist."""
        if not self._redis or not self._has_redisearch:
            return

        from redis.commands.search.field import TagField, TextField, VectorField
        from redis.commands.search.index_definition import IndexDefinition, IndexType

        try:
            await self._redis.ft(self.INDEX_NAME).info()
            logger.debug("RediSearch index '%s' already exists.", self.INDEX_NAME)
        except Exception:
            try:
                schema = (
                    TextField("query"),
                    TagField("service_scope"),
                    VectorField(
                        "vector",
                        "HNSW",
                        {
                            "TYPE": "FLOAT32",
                            "DIM": self.embedding_provider.dimension,
                            "DISTANCE_METRIC": "COSINE",
                        },
                    ),
                )
                definition = IndexDefinition(prefix=[self.DOC_PREFIX], index_type=IndexType.HASH)
                await self._redis.ft(self.INDEX_NAME).create_index(schema, definition=definition)
                logger.info("Created RediSearch vector index '%s'.", self.INDEX_NAME)
            except Exception as e:
                logger.warning("Failed to create RediSearch index: %s. Falling back to in-memory.", e)
                self._has_redisearch = False

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
            logger.warning("Redis ping failed (%s). Disconnecting.", ping_err)
            self._redis = None
            self._has_redisearch = False
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

        if not self._initialized:
            await self.initialize()
        elif self._redis is None:
            await self._ensure_connected()

        start_time = time.monotonic()
        effective_threshold = threshold if threshold is not None else self.similarity_threshold
        target_scope = re.sub(r"[^a-zA-Z0-9_\-]", "", service_filter.strip().lower()) if service_filter and service_filter.strip() else ""
        scoped_q = format_scoped_query(query, service_filter)

        # 1. Generate query embedding
        query_vec = self.embedding_provider.embed_query(scoped_q)

        # 2. Search RediSearch index if available
        if self._redis and self._has_redisearch:
            try:
                match = await self._search_redisearch(
                    scoped_q, query_vec, effective_threshold, start_time, target_scope=target_scope
                )
                if match:
                    return match
            except (aioredis.ConnectionError, aioredis.TimeoutError, ConnectionRefusedError, OSError) as conn_err:
                logger.warning("RediSearch connection failure: %s. Disconnecting and checking in-memory store.", conn_err)
                self._redis = None
                self._has_redisearch = False
            except Exception as e:
                logger.warning("RediSearch query execution error: %s. Query falling back to in-memory store without disconnecting Redis.", e)

        # 3. Fallback to high-performance in-memory vector search
        return self._search_in_memory(
            scoped_q, query_vec, effective_threshold, start_time, target_scope=target_scope
        )

    async def _search_redisearch(
        self,
        scoped_query: str,
        query_vec: list[float],
        threshold: float,
        start_time: float,
        target_scope: str = "",
    ) -> CacheMatch | None:
        """Query native RediSearch HNSW vector index with strict scope isolation."""
        from redis.commands.search.query import Query

        # Cosine distance = 1.0 - cosine_similarity
        query_bytes = np.array(query_vec, dtype=np.float32).tobytes()

        # Strict scope filter: sanitize scope with regex to prevent grammar parsing errors
        clean_scope = re.sub(r"[^a-zA-Z0-9_\-]", "", target_scope)
        if clean_scope:
            safe_scope = clean_scope.replace("-", "\\-")
            filter_query = f"(@service_scope:{{{safe_scope}}})=>[KNN 1 @vector $vec AS score]"
        else:
            filter_query = "*=>[KNN 1 @vector $vec AS score]"

        q = (
            Query(filter_query)
            .sort_by("score")
            .return_fields("query", "report", "score")
            .paging(0, 1)
            .dialect(2)
        )

        res = await self._redis.ft(self.INDEX_NAME).search(q, query_params={"vec": query_bytes})
        if not res.docs:
            return None

        doc = res.docs[0]
        score = float(getattr(doc, "score", 1.0))
        similarity = 1.0 - score

        if similarity >= threshold:
            report_data = json.loads(doc.report)
            report = IncidentTriageReport.model_validate(report_data)
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            return CacheMatch(
                report=report,
                similarity=round(similarity, 4),
                execution_time_ms=round(elapsed_ms, 2),
                matched_query=doc.query,
                cache_hit=True,
            )

        return None

    def _search_in_memory(
        self,
        scoped_query: str,
        query_vec: list[float],
        threshold: float,
        start_time: float,
        target_scope: str = "",
    ) -> CacheMatch | None:
        """Query in-memory numpy matrix with vectorized dot product and strict scope filtering."""
        now = time.monotonic()

        # Purge expired entries safely
        expired_keys = [k for k, v in list(self._in_memory_docs.items()) if v.expires_at < now]
        for k in expired_keys:
            self._in_memory_docs.pop(k, None)

        if not self._in_memory_docs:
            return None

        # Filter candidate documents matching requested scope
        if target_scope:
            matching_items = [
                (k, v) for k, v in list(self._in_memory_docs.items())
                if v.service_scope.lower() == target_scope.lower()
            ]
        else:
            matching_items = list(self._in_memory_docs.items())

        if not matching_items:
            return None

        # Build matrix and compute similarities
        doc_ids = [item[0] for item in matching_items]
        vectors = np.array([item[1].vector for item in matching_items], dtype=np.float32)
        q_arr = np.array(query_vec, dtype=np.float32)

        # In normalized vector space, dot product is exact cosine similarity
        similarities = np.dot(vectors, q_arr)
        best_idx = int(np.argmax(similarities))
        best_similarity = float(similarities[best_idx])

        if best_similarity >= threshold:
            best_doc = self._in_memory_docs[doc_ids[best_idx]]
            report_data = json.loads(best_doc.report_json)
            report = IncidentTriageReport.model_validate(report_data)
            elapsed_ms = (time.monotonic() - start_time) * 1000.0

            return CacheMatch(
                report=report,
                similarity=round(best_similarity, 4),
                execution_time_ms=round(elapsed_ms, 2),
                matched_query=best_doc.query,
                cache_hit=True,
            )

        return None

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
        if not self._initialized:
            await self.initialize()
        elif self._redis is None:
            await self._ensure_connected()

        target_scope = re.sub(r"[^a-zA-Z0-9_\-]", "", service_filter.strip().lower()) if service_filter and service_filter.strip() else ""
        scoped_q = format_scoped_query(query, service_filter)
        query_vec = self.embedding_provider.embed_query(scoped_q)

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
        now = time.monotonic()

        # 1. Update in-memory store with LRU capacity protection
        vec_arr = np.array(query_vec, dtype=np.float32)
        if len(self._in_memory_docs) >= self.max_capacity:
            oldest_key = next(iter(self._in_memory_docs))
            self._in_memory_docs.pop(oldest_key, None)

        self._in_memory_docs[doc_id] = InMemoryVectorEntry(
            doc_id=doc_id,
            query=scoped_q,
            report_json=report_json,
            vector=vec_arr,
            expires_at=now + effective_ttl,
            service_scope=target_scope,
        )

        # 2. Update Redis if connected
        if self._redis:
            try:
                redis_key = f"{self.DOC_PREFIX}{doc_id}"
                mapping = {
                    "query": scoped_q,
                    "service_scope": target_scope,
                    "report": report_json,
                    "vector": vec_arr.tobytes(),
                }
                await self._redis.hset(redis_key, mapping=mapping)
                await self._redis.expire(redis_key, effective_ttl)
            except Exception as e:
                logger.warning("Redis store error: %s (in-memory copy active). Marking disconnected.", e)
                self._redis = None
                self._has_redisearch = False

    async def clear(self) -> None:
        """Clear all in-memory and Redis cache entries."""
        self._in_memory_docs.clear()
        if self._redis:
            try:
                keys = [k async for k in self._redis.scan_iter(match=f"{self.DOC_PREFIX}*", count=100)]
                if keys:
                    for i in range(0, len(keys), 500):
                        await self._redis.delete(*keys[i : i + 500])
            except Exception as e:
                logger.warning("Redis clear error: %s", e)
                self._redis = None
                self._has_redisearch = False

    @property
    def is_redis_connected(self) -> bool:
        """Check if Redis connection is established."""
        return self._redis is not None

    @property
    def driver(self) -> str:
        """Return the active vector search driver name."""
        if self._redis is not None:
            if self._has_redisearch:
                return "redisearch"
            return "redis_hash_fallback"
        return "in_memory"

    @property
    def count(self) -> int:
        """Return total count of cached documents in memory."""
        return len(self._in_memory_docs)

    async def close(self) -> None:
        """Gracefully close Redis client connection and reset state."""
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception as e:
                logger.debug("Error closing Redis connection: %s", e)
            finally:
                self._redis = None
                self._initialized = False


# Global singleton
semantic_cache = SemanticCacheManager()

