"""Triage gateway: the POST /triage request path, and the only request-path cache writer.

`triage()` records the request path's execution trace (ADR-008) on the recorder it is given,
hands that recorder to the graph run, and builds the `TriageResponse` that both `POST /triage`
and the closing `trace_finished` event carry, so every route answers with the same response.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, assert_never, cast

from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings
from resilient_triage.graph.nodes.fallback_report import (
    build_fallback_report,
    fallback_report_from_state,
)
from resilient_triage.graph.reconciliation import DegradationEvidence, reconcile
from resilient_triage.schemas.api import TriageResponse
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.state import TriageState
from resilient_triage.trace.events import (
    CacheLookup,
    CacheStore,
    DeadlineExceeded,
    Reconciled,
    RunSlot,
    TraceFinished,
    TraceStarted,
)
from resilient_triage.trace.fields import CacheBackend, TriageOutcomeKind
from resilient_triage.trace.recorder import ListTrace, NullTrace, TraceRecorder, record_event
from resilient_triage.trace.summary import TraceSummary

logger = logging.getLogger(__name__)

_GRAPH_EXCEPTION_REASON = (
    "Triage state machine failed unexpectedly. Safe heuristic fallback engaged."
)
_DEADLINE_REASON = (
    "Run deadline exceeded before analysis finished. Safe heuristic fallback engaged."
)

StoreOutcome = Literal["stored", "skipped_fallback", "failed"]


class GraphRunner(Protocol):
    """Runs the triage graph on an input state, recording on `trace`; returns its final state."""

    async def __call__(
        self, run_input: TriageState, /, *, trace: TraceRecorder
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CacheHit:
    """Answered from the semantic cache."""

    response: TriageResponse

    @property
    def report(self) -> IncidentTriageReport:
        """The cached report."""
        return self.response.report

    @property
    def similarity(self) -> float:
        """Cosine similarity of the matched cache entry."""
        return self.response.cache_similarity or 0.0


@dataclass(frozen=True)
class Analyzed:
    """Answered by a run; the report may be a synthesis fallback, as its own status says."""

    response: TriageResponse

    @property
    def report(self) -> IncidentTriageReport:
        """The run's report."""
        return self.response.report


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
class _Answer:
    """How the request path ended, before its response is built; no report when shed."""

    kind: TriageOutcomeKind
    report: IncidentTriageReport | None = None
    similarity: float | None = None


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
        self,
        query: str,
        scope: str | None,
        *,
        force_refresh: bool = False,
        trace: TraceRecorder | None = None,
    ) -> TriageOutcome:
        """Return how this request ended, counting it and recording its trace on `trace`.

        Unless cancelled, a request ends in exactly one `trace_finished` carrying the
        outcome's response (None when shed). A cancellation propagates, unrecorded.
        """
        recorder: TraceRecorder = trace if trace is not None else NullTrace()
        started = time.monotonic()
        record_event(
            recorder, lambda: TraceStarted(scoped=bool(scope), force_refresh=force_refresh)
        )
        answer = await self._answer(query, scope, force_refresh=force_refresh, trace=recorder)
        outcome = _outcome(answer, started, recorder)
        self._count(outcome)
        response = None if isinstance(outcome, Shed) else outcome.response
        record_event(recorder, lambda: TraceFinished(outcome=answer.kind, response=response))
        return outcome

    def _count(self, outcome: TriageOutcome) -> None:
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

    async def _answer(
        self, query: str, scope: str | None, *, force_refresh: bool, trace: TraceRecorder
    ) -> _Answer:
        """Answer from the cache, else run the graph in a slot and store its report, else shed."""
        if not force_refresh:
            lookup_started = time.monotonic()
            match = await self._cache.lookup(query=query, service_filter=scope)
            lookup_ms = _ms_since(lookup_started)
            record_event(
                trace,
                lambda: CacheLookup(
                    backend=self._backend(),
                    hit=match is not None,
                    similarity=match.similarity if match else None,
                    threshold=self._cache.similarity_threshold,
                    duration_ms=lookup_ms,
                ),
            )
            if match:
                return _Answer("cache_hit", match.report, similarity=match.similarity)
        if not await self._acquire_run_slot(trace):
            logger.warning("No run slot freed up in time; shed request.")
            return _Answer("shed")
        try:
            analysis = await self._analyze(query, scope, trace)
        finally:
            self._run_slots.release()
        await self._store(query, scope, analysis, trace)
        return _Answer("analyzed", analysis.report)

    async def _acquire_run_slot(self, trace: TraceRecorder) -> bool:
        """Wait up to the queue timeout for a run slot; record the wait and whether it came."""
        wait_started = time.monotonic()
        try:
            await asyncio.wait_for(
                self._run_slots.acquire(), timeout=self._settings.triage_queue_timeout_s
            )
            acquired = True
        except TimeoutError:
            acquired = False
        waited_ms = _ms_since(wait_started)
        capacity = self._settings.triage_max_concurrent_runs
        record_event(
            trace, lambda: RunSlot(acquired=acquired, waited_ms=waited_ms, capacity=capacity)
        )
        return acquired

    async def _store(
        self, query: str, scope: str | None, analysis: _Analysis, trace: TraceRecorder
    ) -> None:
        """Cache the run's report unless it fell back; a failing store is logged, not raised."""
        store_started = time.monotonic()
        stored: StoreOutcome = "skipped_fallback"
        if not analysis.fell_back:
            try:
                await self._cache.store(query=query, report=analysis.report, service_filter=scope)
                stored = "stored"
            except Exception:
                logger.exception("Caching the run's report failed; answering it uncached.")
                stored = "failed"
        store_ms = _ms_since(store_started)
        record_event(
            trace,
            lambda: CacheStore(backend=self._backend(), outcome=stored, duration_ms=store_ms),
        )

    def _backend(self) -> CacheBackend:
        """The cache's active driver; an unknown name fails the event, which is then dropped."""
        return cast(CacheBackend, self._cache.driver)

    async def _analyze(self, query: str, scope: str | None, trace: TraceRecorder) -> _Analysis:
        """Run the graph; return its report, or a synthesis fallback, and whether it fell back."""
        deadline_s = self._settings.triage_run_deadline_s
        deadline = asyncio.timeout(deadline_s)
        try:
            async with deadline:
                state = await self._run_graph(
                    {"query": query, "service_filter": scope}, trace=trace
                )
        except Exception as exc:
            if isinstance(exc, TimeoutError) and deadline.expired():
                logger.warning("Run deadline exceeded; answering with a synthesis fallback.")
                record_event(trace, lambda: DeadlineExceeded(deadline_s=deadline_s))
                report = _reconciled_fallback(query, _DEADLINE_REASON, trace)
                return _Analysis(report, fell_back=True)
            logger.exception("Triage graph raised; answering with a synthesis fallback.")
            report = _reconciled_fallback(query, _GRAPH_EXCEPTION_REASON, trace)
            return _Analysis(report, fell_back=True)
        final_report: IncidentTriageReport | None = state.get("final_report")
        if final_report is None:
            return _Analysis(_synthesis_fallback(query, state, trace), fell_back=True)
        evidence = DegradationEvidence.from_state(cast(TriageState, state))
        return _Analysis(final_report, fell_back=evidence.synthesis_fell_back)


def _outcome(answer: _Answer, started: float, trace: TraceRecorder) -> TriageOutcome:
    """Build the outcome and its response; the summary covers every event recorded so far."""
    if answer.report is None:
        return Shed()
    response = TriageResponse(
        report=answer.report,
        cached=answer.kind == "cache_hit",
        cache_similarity=answer.similarity,
        execution_time_ms=round(_ms_since(started), 2),
        trace=_summary(trace),
    )
    return CacheHit(response) if answer.kind == "cache_hit" else Analyzed(response)


def _summary(trace: TraceRecorder) -> TraceSummary | None:
    """The trace summary of a recorder that keeps its events, else None; never raises."""
    if not isinstance(trace, ListTrace):
        return None
    try:
        return trace.summary()
    except Exception:
        logger.exception("Summarizing the execution trace failed; answering without it.")
        return None


def _ms_since(started: float) -> float:
    """Milliseconds since `started`, rounded to microseconds like the recorder's t_ms."""
    return round(max((time.monotonic() - started) * 1000.0, 0.0), 3)


def _synthesis_fallback(
    query: str, state: dict[str, Any], trace: TraceRecorder
) -> IncidentTriageReport:
    """Build and reconcile a synthesis fallback for a run that produced no final report."""
    logger.warning("Graph produced no final report. Engaging emergency fallback report.")
    run_state = cast(TriageState, state)
    report = fallback_report_from_state(query, run_state)
    evidence = DegradationEvidence.from_state(run_state)
    reconciled = reconcile(report, replace(evidence, synthesis_fell_back=True))
    record_event(trace, lambda: Reconciled.between(report, reconciled))
    return reconciled


def _reconciled_fallback(query: str, reason: str, trace: TraceRecorder) -> IncidentTriageReport:
    """Build a synthesis fallback with no run evidence, reconcile it and record the change."""
    report = build_fallback_report(query, reason=reason)
    evidence = DegradationEvidence(
        tripped_breakers=frozenset(), unavailable_sources=frozenset(), synthesis_fell_back=True
    )
    reconciled = reconcile(report, evidence)
    record_event(trace, lambda: Reconciled.between(report, reconciled))
    return reconciled
