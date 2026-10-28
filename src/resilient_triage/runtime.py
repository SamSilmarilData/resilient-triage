"""AppRuntime: one object owning the app's stateful modules, built fresh per app or per test."""

import logging
from dataclasses import dataclass
from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from resilient_triage.cache.embeddings import BaseEmbeddingProvider
from resilient_triage.cache.semantic_cache import CacheMatch, SemanticCacheManager
from resilient_triage.config import Settings
from resilient_triage.graph.llm import GroqQuotaTracker, build_triage_llm, new_groq_http_client
from resilient_triage.resilience.circuit_breaker import CircuitBreakerInfo, CircuitBreakerRegistry
from resilient_triage.schemas.api import LLMQuotaSnapshot
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.telemetry.chaos import ChaosClient, ChaosManager
from resilient_triage.telemetry.statuspage import (
    SUMMARY_CACHE_TTL_SECONDS,
    StatuspageClient,
    new_statuspage_http_client,
)

logger = logging.getLogger(__name__)


class CacheReader(Protocol):
    """Read-only view of the semantic cache: the only cache access a graph node gets."""

    async def lookup(
        self, query: str, service_filter: str | None, threshold: float
    ) -> CacheMatch | None: ...


class CacheWriter(Protocol):
    """Write side of the semantic cache (temporary: the Triage gateway, #17, takes it over)."""

    async def store(
        self, query: str, report: IncidentTriageReport, service_filter: str | None = None
    ) -> None: ...


@dataclass(frozen=True)
class TelemetrySources:
    """Today's telemetry clients, bound to one runtime's chaos manager and breakers."""

    statuspage: StatuspageClient
    chaos: ChaosClient
    breakers: CircuitBreakerRegistry


@dataclass(frozen=True)
class TriageDeps:
    """Everything a graph node can see (LangGraph context_schema)."""

    llm: BaseChatModel
    telemetry: TelemetrySources
    history: CacheReader
    cache_writer: CacheWriter


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
        quota: GroqQuotaTracker,
        deps: TriageDeps,
        active_model: str,
    ) -> None:
        self.cache = cache
        self.breakers = breakers
        self.chaos = chaos
        self.quota = quota
        self.deps = deps
        self.active_model = active_model

    async def start(self) -> None:
        """Connect the cache (or fall back to memory) and register the default breakers."""
        try:
            await self.cache.initialize()
        except Exception as exc:
            logger.warning("Semantic cache initialization failed: %s", exc)
        self.breakers.get_or_create("statuspage")
        self.breakers.get_or_create("chaos")

    async def aclose(self) -> None:
        """Close every client this runtime owns."""

    async def snapshot(self) -> ResilienceSnapshot:
        """Return the current resilience state."""
        try:
            cache_connected = await self.cache.check_connection()
        except Exception as exc:
            logger.warning("Cache connectivity check failed: %s", exc)
            cache_connected = False
        return ResilienceSnapshot(
            breakers=self.breakers.get_all_statuses(),
            cache_driver=self.cache.driver,
            cache_connected=cache_connected,
            cache_entries=self.cache.count,
            chaos_active=self.chaos.get_config().active,
            active_model=self.active_model,
            llm_quota=self.quota.get_snapshot(),
        )


def build_runtime(
    settings: Settings,
    *,
    llm_adapter: BaseChatModel | None = None,
    embedder: BaseEmbeddingProvider | None = None,
) -> AppRuntime:
    """Construct a runtime with fresh stateful modules; performs no I/O."""
    cache = SemanticCacheManager(redis_url=settings.redis_url, embedding_provider=embedder)
    breakers = CircuitBreakerRegistry()
    chaos = ChaosManager()
    quota = GroqQuotaTracker()
    statuspage = StatuspageClient(
        url=settings.statuspage_url,
        client=new_statuspage_http_client(),
        cache_ttl_seconds=SUMMARY_CACHE_TTL_SECONDS,
        summary_cache={},
        breaker_registry=breakers,
    )
    if llm_adapter is not None:
        llm = llm_adapter
        active_model = f"Test Override ({llm_adapter._llm_type})"
    else:
        llm, active_model = build_triage_llm(
            settings, quota_tracker=quota, http_client=new_groq_http_client()
        )
    deps = TriageDeps(
        llm=llm,
        telemetry=TelemetrySources(
            statuspage=statuspage,
            chaos=ChaosClient(manager=chaos, breaker_registry=breakers),
            breakers=breakers,
        ),
        history=cache,
        cache_writer=cache,
    )
    return AppRuntime(
        cache=cache,
        breakers=breakers,
        chaos=chaos,
        quota=quota,
        deps=deps,
        active_model=active_model,
    )
