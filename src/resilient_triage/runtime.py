"""AppRuntime: one object owning the app's stateful modules, built fresh per app or per test."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, TypeVar

import httpx

from resilient_triage.cache.embeddings import BaseEmbeddingProvider
from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings
from resilient_triage.graph.builder import triage_graph
from resilient_triage.graph.deps import CacheReader, TelemetrySources, TriageDeps
from resilient_triage.llm import LLMGateway, ModelAdapter, build_llm_gateway
from resilient_triage.resilience.circuit_breaker import CircuitBreakerInfo, CircuitBreakerRegistry
from resilient_triage.schemas.api import LLMQuotaSnapshot
from resilient_triage.schemas.state import TriageState
from resilient_triage.schemas.telemetry import ChaosTelemetryResponse
from resilient_triage.telemetry.chaos import ChaosClient, ChaosManager
from resilient_triage.telemetry.statuspage import (
    SUMMARY_CACHE_TTL_SECONDS,
    StatuspageClient,
    new_statuspage_http_client,
)
from resilient_triage.triage_gateway import TriageGateway

# Re-exported: the run-context contract lives in graph.deps so nodes avoid an import cycle.
__all__ = [
    "AppRuntime",
    "CacheReader",
    "ResilienceSnapshot",
    "TelemetrySources",
    "TriageDeps",
    "build_runtime",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Embedded at start() so the first demo requests hit a warm model and embedding LRU.
WARMUP_QUERIES: tuple[str, ...] = (
    "warmup telemetry query",
    "Internal API gateway service failing with 503 outage and elevated probe latency",
    (
        "[scope: internal] Internal API gateway service failing with 503 outage and "
        "elevated probe latency"
    ),
    "Internal payment processing failing with 503 service unavailable",
    "[scope: internal] Internal payment processing failing with 503 service unavailable",
    "GitHub Actions workflows are failing with 503 and high webhook latency",
    "[scope: actions] GitHub Actions workflows are failing with 503 and high webhook latency",
    "GitHub Actions workflows failing with 503 service unavailable",
    "GitHub Actions jobs are failing with 503 service unavailable errors",
    "Stripe webhook ingestion failing with 504 gateway timeout",
    "[scope: billing] Stripe webhook ingestion failing with 504 gateway timeout",
    "PostgreSQL primary connection pool exhausted under spike",
    "[scope: database] PostgreSQL primary connection pool exhausted under spike",
)


@dataclass(frozen=True)
class ResilienceSnapshot:
    """Point-in-time view of breakers, chaos, cache and LLM quota for one runtime."""

    breakers: dict[str, CircuitBreakerInfo]
    cache_driver: str
    cache_connected: bool
    cache_entries: int
    chaos_active: bool
    active_model: str
    llm_quota: LLMQuotaSnapshot


class AppRuntime:
    """The app's stateful modules, with a start/aclose lifecycle and a resilience snapshot."""

    def __init__(
        self,
        *,
        cache: SemanticCacheManager,
        breakers: CircuitBreakerRegistry,
        chaos: ChaosManager,
        llm: LLMGateway,
        deps: TriageDeps,
        settings: Settings,
        http_clients: tuple[httpx.AsyncClient, ...] = (),
    ) -> None:
        self.cache = cache
        self.breakers = breakers
        self.chaos = chaos
        self.llm = llm
        self.deps = deps
        self._http_clients = http_clients
        self.triage_gateway = TriageGateway(cache, self.run_graph, settings)

    async def start(self) -> None:
        """Connect the cache (else use memory) and register default breakers; never raises."""
        try:
            await self.cache.initialize()
        except Exception as exc:
            logger.warning("Semantic cache initialization failed: %s", exc)
        try:
            for query in WARMUP_QUERIES:
                self.cache.embedding_provider.embed_query(query)
            logger.info(
                "Pre-warmed %d incident presets in embedding LRU cache.", len(WARMUP_QUERIES)
            )
        except Exception as exc:
            logger.warning("Embedding pre-warmup warning: %s", exc)
        for name in ("statuspage", "chaos"):
            _guarded(
                f"Registering the {name} breaker", partial(self.breakers.get_or_create, name), None
            )

    async def run_graph(self, inp: TriageState) -> dict[str, Any]:
        """Run the triage graph with this runtime's deps as its context (the gateway's runner)."""
        return await triage_graph.ainvoke(inp, context=self.deps)

    async def probe_cluster_sentinel(self) -> ChaosTelemetryResponse:
        """Probe Cluster Sentinel through this runtime's retries and breaker; raises on failure."""
        return await self.deps.telemetry.chaos.probe()

    async def aclose(self) -> None:
        """Close every client this runtime owns."""
        try:
            await self.cache.close()
        except Exception as exc:
            logger.warning("Semantic cache close failed: %s", exc)
        await self.llm.aclose()
        for client in self._http_clients:
            try:
                await client.aclose()
            except Exception as exc:
                logger.warning("HTTP client close failed: %s", exc)

    async def snapshot(self) -> ResilienceSnapshot:
        """Return the current resilience state; never raises, degrading any unreadable member."""
        try:
            cache_connected = await self.cache.check_connection()
        except Exception as exc:
            logger.warning("Cache connectivity check failed: %s", exc)
            cache_connected = False
        return ResilienceSnapshot(
            breakers=_guarded("Reading breaker statuses", self.breakers.get_all_statuses, {}),
            # driver and count are plain in-memory reads with no failure path.
            cache_driver=self.cache.driver,
            cache_connected=cache_connected,
            cache_entries=self.cache.count,
            chaos_active=_guarded(
                "Reading the chaos config", lambda: self.chaos.get_config().active, False
            ),
            active_model=self.llm.label,
            llm_quota=_guarded(
                "Reading the LLM quota",
                self.llm.snapshot,
                LLMQuotaSnapshot(active_model=self.llm.primary),
            ),
        )


def _guarded(what: str, read: Callable[[], T], default: T) -> T:
    """Return read(), or log and return default if it raises."""
    try:
        return read()
    except Exception as exc:
        logger.warning("%s failed: %s", what, exc)
        return default


def build_runtime(
    settings: Settings,
    *,
    llm_adapter: ModelAdapter | None = None,
    embedder: BaseEmbeddingProvider | None = None,
    statuspage_http: httpx.AsyncClient | None = None,
) -> AppRuntime:
    """Construct a runtime with fresh stateful modules; performs no I/O."""
    cache = SemanticCacheManager(redis_url=settings.redis_url, embedding_provider=embedder)
    breakers = CircuitBreakerRegistry()
    chaos = ChaosManager()
    llm = build_llm_gateway(settings, llm_adapter)
    if statuspage_http is None:
        statuspage_http = new_statuspage_http_client()
    http_clients: list[httpx.AsyncClient] = [statuspage_http]
    statuspage = StatuspageClient(
        url=settings.statuspage_url,
        client=statuspage_http,
        cache_ttl_seconds=SUMMARY_CACHE_TTL_SECONDS,
        breaker_registry=breakers,
    )
    deps = TriageDeps(
        llm=llm,
        telemetry=TelemetrySources(
            statuspage=statuspage,
            chaos=ChaosClient(manager=chaos, breaker_registry=breakers),
            breakers=breakers,
        ),
        history=cache,
    )
    return AppRuntime(
        cache=cache,
        breakers=breakers,
        chaos=chaos,
        llm=llm,
        deps=deps,
        settings=settings,
        http_clients=tuple(http_clients),
    )
