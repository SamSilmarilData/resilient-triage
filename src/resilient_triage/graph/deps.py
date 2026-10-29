"""TriageDeps: the collaborators a graph node can see, passed as LangGraph's run context.

Kept free of the runtime and graph modules so nodes can import it without an import cycle;
`resilient_triage.runtime` builds it and re-exports these names.
"""

from dataclasses import dataclass
from typing import Protocol

from resilient_triage.cache.semantic_cache import CacheMatch
from resilient_triage.llm import LLMGateway
from resilient_triage.telemetry.probe import TelemetryProbe


class CacheReader(Protocol):
    """Read-only view of the semantic cache: the only cache access a graph node gets."""

    async def lookup(
        self, query: str, service_filter: str | None, threshold: float
    ) -> CacheMatch | None: ...


@dataclass(frozen=True)
class TriageDeps:
    """Everything a graph node can see (LangGraph context_schema)."""

    llm: LLMGateway
    telemetry: TelemetryProbe
    history: CacheReader
