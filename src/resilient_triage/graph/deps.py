"""TriageDeps: the collaborators a graph node can see, passed as LangGraph's run context.

Kept free of the runtime and graph modules so nodes can import it without an import cycle;
`resilient_triage.runtime` builds it and re-exports these names.
"""

from dataclasses import dataclass
from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from resilient_triage.cache.semantic_cache import CacheMatch
from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.telemetry.chaos import ChaosClient
from resilient_triage.telemetry.statuspage import StatuspageClient


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
    """Today's telemetry sources, bound to one runtime's chaos manager and breakers."""

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
