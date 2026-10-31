"""TriageDeps: the collaborators a graph node can see, passed as LangGraph's run context.

Kept free of the runtime and graph modules so nodes can import it without an import cycle;
`resilient_triage.runtime` builds it and re-exports these names.
"""

from dataclasses import dataclass, field
from typing import Protocol

from resilient_triage.cache.semantic_cache import CacheMatch
from resilient_triage.llm import LLMGateway
from resilient_triage.telemetry.probe import TelemetryProbe
from resilient_triage.trace.recorder import NullTrace, TraceRecorder


class CacheReader(Protocol):
    """Read-only view of the semantic cache: the only cache access a graph node gets."""

    async def lookup(
        self, query: str, service_filter: str | None, threshold: float
    ) -> CacheMatch | None: ...


@dataclass(frozen=True)
class TriageDeps:
    """Everything a graph node can see (LangGraph context_schema).

    One instance is shared by every run, so it is never mutated: a traced run gets its own
    copy, `dataclasses.replace(deps, trace=recorder)` (ADR-008).
    """

    llm: LLMGateway
    telemetry: TelemetryProbe
    history: CacheReader
    trace: TraceRecorder = field(default_factory=NullTrace)
