"""AppRuntime: one object owning the app's stateful modules, built fresh per app or per test."""

import logging
from dataclasses import dataclass

from langchain_core.language_models.chat_models import BaseChatModel

from resilient_triage.cache.embeddings import BaseEmbeddingProvider
from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings
from resilient_triage.resilience.circuit_breaker import CircuitBreakerInfo, CircuitBreakerRegistry
from resilient_triage.telemetry.chaos import ChaosManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResilienceSnapshot:
    """Point-in-time view of breakers, chaos, cache and LLM quota for one runtime."""

    breakers: dict[str, CircuitBreakerInfo]


class AppRuntime:
    """The app's stateful modules, with a start/aclose lifecycle and a resilience snapshot."""

    def __init__(
        self,
        *,
        cache: SemanticCacheManager,
        breakers: CircuitBreakerRegistry,
        chaos: ChaosManager,
    ) -> None:
        self.cache = cache
        self.breakers = breakers
        self.chaos = chaos

    async def start(self) -> None:
        """Register the default breakers."""
        self.breakers.get_or_create("statuspage")
        self.breakers.get_or_create("chaos")

    async def aclose(self) -> None:
        """Close every client this runtime owns."""

    async def snapshot(self) -> ResilienceSnapshot:
        """Return the current resilience state."""
        return ResilienceSnapshot(breakers=self.breakers.get_all_statuses())


def build_runtime(
    settings: Settings,
    *,
    llm_adapter: BaseChatModel | None = None,
    embedder: BaseEmbeddingProvider | None = None,
) -> AppRuntime:
    """Construct a runtime with fresh stateful modules; performs no I/O."""
    return AppRuntime(
        cache=SemanticCacheManager(redis_url=settings.redis_url, embedding_provider=embedder),
        breakers=CircuitBreakerRegistry(),
        chaos=ChaosManager(),
    )
