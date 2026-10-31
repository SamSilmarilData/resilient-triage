"""Trace recorders: the per-request source of an execution trace (ADR-008).

A recorder is made per request and passed explicitly (never a module-level singleton).
`record()` is synchronous and never raises, so recording can't change a run's result.

A leaf module at import time (events and the summary are imported only for type checking,
or lazily), so `llm.gateway`, which `schemas` imports, can default to `NullTrace` without
an import cycle through `trace.events` -> `schemas.api` -> `llm`.
"""

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from resilient_triage.trace.events import TraceEvent
    from resilient_triage.trace.summary import TraceSummary

logger = logging.getLogger(__name__)

TraceSink = Callable[["TraceEvent"], None]


class TraceRecorder(Protocol):
    """Where a request's trace events go; record() never raises."""

    def record(self, event: "TraceEvent") -> None:
        """Record one event, stamping its seq and t_ms; never raises."""
        ...


class NullTrace:
    """Records nothing: the default for callers outside a traced request."""

    def record(self, event: "TraceEvent") -> None:
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
    def events(self) -> "tuple[TraceEvent, ...]":
        """The events recorded so far, in seq order."""
        return tuple(self._events)

    def summary(self) -> "TraceSummary":
        """The trace summary of the events recorded so far."""
        from resilient_triage.trace.summary import summarize

        return summarize(self._events)

    def record(self, event: "TraceEvent") -> None:
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


def record_event(trace: TraceRecorder, build: "Callable[[], TraceEvent]") -> None:
    """Build an event and record it; never raises, so tracing can't change a run's result.

    Instrumented code calls this rather than `trace.record()`: it also guards the event's
    construction, and a recorder that breaks the never-raises contract.
    """
    try:
        trace.record(build())
    except Exception:
        logger.exception("Dropped a trace event.")
