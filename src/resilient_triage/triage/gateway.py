"""Triage gateway: the POST /triage request path, and the only request-path cache writer."""

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol, assert_never, cast

from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings
from resilient_triage.graph.nodes.fallback_report import (
    build_fallback_report,
    fallback_report_from_state,
)
from resilient_triage.graph.reconciliation import DegradationEvidence, reconcile
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.state import TriageState

logger = logging.getLogger(__name__)


class GraphRunner(Protocol):
    """Runs the triage graph on an input state and returns its final state."""

    async def __call__(self, run_input: TriageState, /) -> dict[str, Any]: ...


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


@dataclass(frozen=True)
class _Analysis:
    """A run's report, and whether it is a synthesis fallback that must not be cached."""

    report: IncidentTriageReport
    fell_back: bool


@dataclass(frozen=True)
class TriageMetrics:
    """Counts of triage outcomes, exported on /metrics as cache=hit/miss/saturated."""

    hit: int = 0
    miss: int = 0
    saturated: int = 0


class TriageGateway:
    """Answers a triage query from the cache, or by a run in a bounded slot, or sheds it."""

    def __init__(
        self, cache: SemanticCacheManager, run_graph: GraphRunner, settings: Settings
    ) -> None:
        self._cache = cache
        self._run_graph = run_graph
        self._settings = settings
        self._run_slots = asyncio.Semaphore(settings.triage_max_concurrent_runs)
        self._metrics = TriageMetrics()

    @property
    def metrics(self) -> TriageMetrics:
        """A snapshot of this gateway's outcome counts."""
        return self._metrics

    async def triage(
        self, query: str, scope: str | None, *, force_refresh: bool = False
    ) -> TriageOutcome:
        """Return how this triage request ended, counting the outcome."""
        outcome = await self._triage(query, scope, force_refresh=force_refresh)
        m = self._metrics
        match outcome:
            case CacheHit():
                self._metrics = replace(m, hit=m.hit + 1)
            case Analyzed():
                self._metrics = replace(m, miss=m.miss + 1)
            case Shed():
                self._metrics = replace(m, saturated=m.saturated + 1)
            case _:
                assert_never(outcome)
        return outcome

    async def _triage(self, query: str, scope: str | None, *, force_refresh: bool) -> TriageOutcome:
        """Answer from the cache, else run the graph in a slot and store its report, else shed."""
        if not force_refresh:
            match = await self._cache.lookup(query=query, service_filter=scope)
            if match:
                return CacheHit(report=match.report, similarity=match.similarity)
        try:
            await asyncio.wait_for(
                self._run_slots.acquire(), timeout=self._settings.triage_queue_timeout_s
            )
        except TimeoutError:
            logger.warning("No run slot freed up in time; shed request.")
            return Shed()
        try:
            analysis = await self._analyze(query, scope)
        finally:
            self._run_slots.release()
        if not analysis.fell_back:
            await self._cache.store(query=query, report=analysis.report, service_filter=scope)
        return Analyzed(report=analysis.report)

    async def _analyze(self, query: str, scope: str | None) -> _Analysis:
        """Run the graph; return its report, or a synthesis fallback, and whether it fell back."""
        deadline = asyncio.timeout(self._settings.triage_run_deadline_s)
        try:
            async with deadline:
                state = await self._run_graph({"query": query, "service_filter": scope})
        except TimeoutError:
            if not deadline.expired():
                logger.exception("Triage graph raised; answering with a synthesis fallback.")
                return _Analysis(_graph_exception_fallback(query), fell_back=True)
            logger.warning("Run deadline exceeded; answering with a synthesis fallback.")
            return _Analysis(_deadline_fallback(query), fell_back=True)
        except Exception:
            logger.exception("Triage graph raised; answering with a synthesis fallback.")
            return _Analysis(_graph_exception_fallback(query), fell_back=True)
        final_report: IncidentTriageReport | None = state.get("final_report")
        if final_report is None:
            return _Analysis(_synthesis_fallback(query, state), fell_back=True)
        evidence = DegradationEvidence.from_state(cast(TriageState, state))
        return _Analysis(final_report, fell_back=evidence.synthesis_fell_back)


def _synthesis_fallback(query: str, state: dict[str, Any]) -> IncidentTriageReport:
    """Build and reconcile a synthesis fallback for a run that produced no final report."""
    logger.warning("Graph produced no final report. Engaging emergency fallback report.")
    run_state = cast(TriageState, state)
    report = fallback_report_from_state(query, run_state)
    evidence = DegradationEvidence.from_state(run_state)
    return reconcile(report, replace(evidence, synthesis_fell_back=True))


def _graph_exception_fallback(query: str) -> IncidentTriageReport:
    """Build and reconcile a synthesis fallback for a run whose graph raised."""
    return _reconciled_fallback(
        query, "Triage state machine failed unexpectedly. Safe heuristic fallback engaged."
    )


def _deadline_fallback(query: str) -> IncidentTriageReport:
    """Build and reconcile a synthesis fallback for a run cancelled by its run deadline."""
    return _reconciled_fallback(
        query, "Run deadline exceeded before analysis finished. Safe heuristic fallback engaged."
    )


def _reconciled_fallback(query: str, reason: str) -> IncidentTriageReport:
    """Build a synthesis fallback with no run evidence and reconcile it."""
    report = build_fallback_report(query, reason=reason)
    evidence = DegradationEvidence(
        tripped_breakers=frozenset(), unavailable_sources=frozenset(), synthesis_fell_back=True
    )
    return reconcile(report, evidence)
