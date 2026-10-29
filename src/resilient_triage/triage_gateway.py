"""Triage gateway: the POST /triage request path, and the only request-path cache writer."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings
from resilient_triage.schemas.incident import IncidentTriageReport

GraphRunner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
        raise NotImplementedError
