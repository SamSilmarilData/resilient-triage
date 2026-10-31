"""Triage gateway: the POST /triage request path, and the only request-path cache writer.

`triage()` records the request path's execution trace (ADR-008) on the recorder it is given,
hands that recorder to the graph run, and builds the `TriageResponse` that both `POST /triage`
and the closing `trace_finished` event carry, so every route answers with the same response.
`stream()` runs `triage()` on a `StreamingTrace` and yields its events live for
`POST /triage/stream`.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Protocol, assert_never, cast

from resilient_triage.cache.semantic_cache import CacheMatch, SemanticCacheManager
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
    TraceEvent,
    TraceFinished,
    TraceStarted,
)
from resilient_triage.trace.fields import StoreOutcome, TriageOutcomeKind
from resilient_triage.trace.recorder import (
    ListTrace,
    NullTrace,
    StreamingTrace,
    TraceRecorder,
    ms_since,
    record_event,
)
from resilient_triage.trace.summary import TraceSummary

logger = logging.getLogger(__name__)

_GRAPH_EXCEPTION_REASON = (
    "Triage state machine failed unexpectedly. Safe heuristic fallback engaged."
)
_DEADLINE_REASON = (
    "Run deadline exceeded before analysis finished. Safe heuristic fallback engaged."
)


class GraphRunner(Protocol):
    """Runs the triage graph on an input state, recording on `trace`; returns its final state."""

    async def __call__(
        self, run_input: TriageState, /, *, trace: TraceRecorder
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CacheHit:
    """Answered from the semantic cache."""

    response: TriageResponse
    similarity: float  # cosine similarity of the matched cache entry

    @property
    def report(self) -> IncidentTriageReport:
        """The cached report."""
        return self.response.report


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


# How the request path ended, before its response is built: a cache match, the run's report,
# or None when shed.
_Answer = CacheMatch | IncidentTriageReport | None


@dataclass(frozen=True)
class TriageMetrics:
    """Counts of triage outcomes (cache=hit/miss/saturated on /metrics), and abandoned requests."""

    hit: int = 0
    miss: int = 0
    saturated: int = 0
    # Not a triage outcome: a request cancelled before it ended, e.g. by a client disconnect.
    abandoned: int = 0


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
        """Return how this request ended, counting it and recording its trace on `trace`."""
        # Unless cancelled, a request ends in exactly one `trace_finished` carrying the
        # outcome's response (None when shed). A cancellation propagates, unrecorded, and is
        # counted as abandoned; the run slot is released and nothing is cached.
        recorder: TraceRecorder = trace if trace is not None else NullTrace()
        started = time.monotonic()
        record_event(
            recorder, lambda: TraceStarted(scoped=bool(scope), force_refresh=force_refresh)
        )
        try:
            answer = await self._answer(query, scope, force_refresh=force_refresh, trace=recorder)
        except asyncio.CancelledError:
            self._count_abandoned()
            raise
        return self._finish(_outcome(answer, started, recorder), recorder)

    @asynccontextmanager
    async def stream(
        self, query: str, scope: str | None, *, force_refresh: bool = False
    ) -> AsyncIterator[AsyncIterator[TraceEvent]]:
        """Run a request; yield its live trace events, the last always its one trace_finished."""
        # The request runs as its own task so events reach the client while it runs. Leaving
        # this context, normally or by cancellation (a client disconnect), cancels that task
        # and waits for it. The context, not the event iterator, owns the task: an iterator
        # parked at a `yield` is only closed whenever it is garbage-collected.
        # A task cancelled before its first step never runs triage(), which counts its own
        # cancellation, so this context counts that one instead: exactly once either way.
        trace = StreamingTrace()
        began = asyncio.Event()
        run = asyncio.create_task(
            self._triage_or_fallback(query, scope, force_refresh, trace, began)
        )
        run.add_done_callback(lambda _: trace.close())
        try:
            yield trace.follow()
        finally:
            try:
                await _cancel_and_wait(run)
            finally:
                if not began.is_set():
                    self._count_abandoned()

    async def _triage_or_fallback(
        self,
        query: str,
        scope: str | None,
        force_refresh: bool,
        trace: StreamingTrace,
        began: asyncio.Event,
    ) -> None:
        """Run triage(); if it raises, still finish the trace with a synthesis fallback."""
        began.set()
        started = time.monotonic()
        try:
            await self.triage(query, scope, force_refresh=force_refresh, trace=trace)
        except Exception:
            # Once a stream's 200 headers are sent an error can't become a 500 (ADR-008).
            # triage() records trace_finished last, so a triage() that raised never recorded it.
            logger.exception("Triage request raised; finishing its stream with a fallback.")
            report = _reconciled_fallback(query, _GRAPH_EXCEPTION_REASON, trace)
            self._finish(Analyzed(_response(report, started, trace, similarity=None)), trace)

    def _finish(self, outcome: TriageOutcome, trace: TraceRecorder) -> TriageOutcome:
        """Count how a request ended and record its one trace_finished; return the outcome."""
        self._count(outcome)
        response = None if isinstance(outcome, Shed) else outcome.response
        kind = _kind(outcome)
        record_event(trace, lambda: TraceFinished(outcome=kind, response=response))
        return outcome

    def _count_abandoned(self) -> None:
        """Count a request cancelled before it ended; it is not a triage outcome."""
        logger.info("Triage request abandoned before it ended (cancelled).")
        self._metrics = replace(self._metrics, abandoned=self._metrics.abandoned + 1)

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
            lookup_ms = ms_since(lookup_started)
            record_event(
                trace,
                lambda: CacheLookup(
                    backend=self._cache.driver,
                    hit=match is not None,
                    similarity=match.similarity if match else None,
                    threshold=self._cache.similarity_threshold,
                    duration_ms=lookup_ms,
                ),
            )
            if match:
                return match
        if not await self._acquire_run_slot(trace):
            logger.warning("No run slot freed up in time; shed request.")
            return None
        try:
            analysis = await self._analyze(query, scope, trace)
        finally:
            self._run_slots.release()
        _raise_if_cancelled()  # a cancelled run caches nothing, even if the graph swallowed it
        await self._store(query, scope, analysis, trace)
        return analysis.report

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
        waited_ms = ms_since(wait_started)
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
        store_ms = ms_since(store_started)
        record_event(
            trace,
            lambda: CacheStore(backend=self._cache.driver, outcome=stored, duration_ms=store_ms),
        )

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


async def _cancel_and_wait(task: "asyncio.Task[None]") -> None:
    """Cancel a task and wait, unbounded, until it finishes; re-raise a cancel of this wait."""
    # A cancelled stream may be re-cancelled at every await (anyio cancel scopes), so the wait
    # repeats until the task is done, then re-raises the first cancellation it caught: no task
    # outlives it. The wait has no bound of its own: it relies on a cancelled run ending.
    task.cancel()
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
    if cancelled is not None:
        raise cancelled


def _raise_if_cancelled() -> None:
    """Raise CancelledError if the current task has a cancellation pending."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def _outcome(answer: _Answer, started: float, trace: TraceRecorder) -> TriageOutcome:
    """Build the outcome and its response; the summary covers every event recorded so far."""
    if answer is None:
        return Shed()
    if isinstance(answer, CacheMatch):
        response = _response(answer.report, started, trace, similarity=answer.similarity)
        return CacheHit(response, similarity=answer.similarity)
    return Analyzed(_response(answer, started, trace, similarity=None))


def _response(
    report: IncidentTriageReport, started: float, trace: TraceRecorder, *, similarity: float | None
) -> TriageResponse:
    """The response for a report; cached exactly when it carries a cache similarity."""
    return TriageResponse(
        report=report,
        cached=similarity is not None,
        cache_similarity=similarity,
        execution_time_ms=round(ms_since(started), 2),
        trace=_summary(trace),
    )


def _kind(outcome: TriageOutcome) -> TriageOutcomeKind:
    """The trace's name for an outcome."""
    match outcome:
        case CacheHit():
            return "cache_hit"
        case Analyzed():
            return "analyzed"
        case Shed():
            return "shed"
        case _:
            assert_never(outcome)


def _summary(trace: TraceRecorder) -> TraceSummary | None:
    """The trace summary of a recorder that keeps its events, else None; never raises."""
    if not isinstance(trace, ListTrace):
        return None
    try:
        return trace.summary()
    except Exception:
        logger.exception("Summarizing the execution trace failed; answering without it.")
        return None


def _synthesis_fallback(
    query: str, state: dict[str, Any], trace: TraceRecorder
) -> IncidentTriageReport:
    """Build and reconcile a synthesis fallback for a run that produced no final report."""
    logger.warning("Graph produced no final report. Engaging emergency fallback report.")
    run_state = cast(TriageState, state)
    report = fallback_report_from_state(query, run_state)
    evidence = DegradationEvidence.from_state(run_state)
    return _reconcile(report, replace(evidence, synthesis_fell_back=True), trace)


def _reconciled_fallback(query: str, reason: str, trace: TraceRecorder) -> IncidentTriageReport:
    """Build a synthesis fallback with no run evidence, reconcile it and record the change."""
    report = build_fallback_report(query, reason=reason)
    evidence = DegradationEvidence(
        tripped_breakers=frozenset(), unavailable_sources=frozenset(), synthesis_fell_back=True
    )
    return _reconcile(report, evidence, trace)


def _reconcile(
    report: IncidentTriageReport, evidence: DegradationEvidence, trace: TraceRecorder
) -> IncidentTriageReport:
    """Reconcile a synthesis fallback outside the graph and record what reconcile changed."""
    reconciled = reconcile(report, evidence)
    record_event(trace, lambda: Reconciled.between(report, reconciled))
    return reconciled
