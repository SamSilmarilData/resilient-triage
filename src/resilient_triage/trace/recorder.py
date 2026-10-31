"""Trace recorders: the per-request source of an execution trace (ADR-008).

A recorder is made per request and passed explicitly (never a module-level singleton).
`record()` is synchronous and never raises, so recording can't change a run's result.
"""

import logging
import time
from collections.abc import Callable
from typing import Protocol

from resilient_triage.trace.events import TraceEvent
from resilient_triage.trace.summary import TraceSummary, summarize

logger = logging.getLogger(__name__)

TraceSink = Callable[[TraceEvent], None]


class TraceRecorder(Protocol):
    """Where a request's trace events go; record() never raises."""

    def record(self, event: TraceEvent) -> None:
        """Record one event, stamping its seq and t_ms; never raises."""
        ...


class NullTrace:
    """Records nothing: the default for callers outside a traced request."""

    def record(self, event: TraceEvent) -> None:
        """Drop the event."""


class ListTrace:
    """Keeps the stamped events in order, and hands each to an optional sink."""

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, sink: TraceSink | None = None
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._started = clock()
        self._events: list[TraceEvent] = []

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        """The events recorded so far, in seq order."""
        return tuple(self._events)

    def summary(self) -> TraceSummary:
        """The trace summary of the events recorded so far."""
        return summarize(self._events)

    def record(self, event: TraceEvent) -> None:
        """Stamp seq and monotonic t_ms, keep the event, then feed the sink; never raises."""
        try:
            t_ms = round((self._clock() - self._started) * 1000, 3)
            if self._events:
                t_ms = max(t_ms, self._events[-1].t_ms)
            stamped = event.model_copy(update={"seq": len(self._events), "t_ms": max(t_ms, 0.0)})
            self._events.append(stamped)
            if self._sink is not None:
                self._sink(stamped)
        except Exception:
            logger.exception("Trace recorder dropped a %s event.", event.kind)
