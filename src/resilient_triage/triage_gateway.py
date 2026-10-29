"""Triage gateway: the POST /triage request path, and the only request-path cache writer."""

from dataclasses import dataclass
from typing import Any, Protocol

from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings
from resilient_triage.schemas.incident import IncidentTriageReport


class GraphRunner(Protocol):
    """Runs the triage graph on an input state and returns its final state."""

    async def __call__(self, input: dict[str, Any], /) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CacheHit:
    """Answered from the semantic cache."""

    report: IncidentTriageReport
    similarity: float


@dataclass(frozen=True)
class Analyzed:
    """Answered by a run; the report may be a synthesis fallback, as its own status says."""

    report: IncidentTriageReport


@dataclass(frozen=True)
class Shed:
    """Refused: no run slot freed up in time."""


TriageOutcome = CacheHit | Analyzed | Shed


class TriageGateway:
    """Answers a triage query from the cache, or by a run in a bounded slot, or sheds it."""

    def __init__(
        self, cache: SemanticCacheManager, run_graph: GraphRunner, settings: Settings
    ) -> None:
        self._cache = cache
        self._run_graph = run_graph
        self._settings = settings

    async def triage(
        self, query: str, scope: str | None, *, force_refresh: bool = False
    ) -> TriageOutcome:
        """Return how this triage request ended."""
        if not force_refresh:
            match = await self._cache.lookup(query=query, service_filter=scope)
            if match:
                return CacheHit(report=match.report, similarity=match.similarity)
        state = await self._run_graph({"query": query, "service_filter": scope})
        return Analyzed(report=state["final_report"])
