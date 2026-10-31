"""Guarded telemetry probe: one breaker, deadline and retry policy over every Telemetry source.

Sources only read and parse; the probe owns everything they share. Each read runs
breaker -> source deadline -> retry -> read, so running out of time is a breaker failure,
and a failed read becomes one degraded signal instead of an exception.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol, TypeVar

from resilient_triage.config import settings
from resilient_triage.resilience.circuit_breaker import (
    AsyncCircuitBreaker,
    BreakerOpen,
    CircuitBreakerRegistry,
)
from resilient_triage.resilience.retry import with_retry
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.telemetry import SIGNAL_STATUS_CIRCUIT_OPEN, SIGNAL_STATUS_UNAVAILABLE
from resilient_triage.trace.events import TelemetryRead
from resilient_triage.trace.fields import BreakerState, ReadOutcome, ReadPhase
from resilient_triage.trace.recorder import NullTrace, TraceRecorder, ms_since, record_event

logger = logging.getLogger(__name__)

T = TypeVar("T")
S = TypeVar("S", bound="TelemetrySource")


class TelemetrySource(Protocol):
    """One Telemetry source: reads and parses, raising on failure; the probe guards it."""

    @property
    def id(self) -> str:
        """Source id, also its breaker name."""
        ...

    @property
    def keywords(self) -> frozenset[str]:
        """Words or phrases that route a query to this source, matched as whole words."""
        ...

    @property
    def display_name(self) -> str:
        """Human name used when describing this source, e.g. in a degradation reason."""
        ...

    async def read(self, query: str, scope: str | None) -> list[TelemetrySignal]:
        """Read and parse this source's signals for the query; raises on any failure."""
        ...

    def degraded_label(self, query: str, scope: str | None) -> str:
        """Signal source name for the one degraded signal this source adds when a read fails."""
        ...


@dataclass(frozen=True)
class TelemetryReading:
    """What one collect() saw: signals plus which targeted sources failed, and breaker states."""

    signals: list[TelemetrySignal]
    tripped_breakers: frozenset[str]
    unavailable_sources: frozenset[str]
    breaker_states: dict[str, str]


@dataclass(frozen=True)
class _Read:
    signals: list[TelemetrySignal]


@dataclass(frozen=True)
class _Tripped:
    """The breaker refused the read, or is open right after the read failed."""

    STATUS: ClassVar[str] = SIGNAL_STATUS_CIRCUIT_OPEN
    signal: TelemetrySignal


@dataclass(frozen=True)
class _Unavailable:
    """The read failed and the breaker is not open afterwards."""

    STATUS: ClassVar[str] = SIGNAL_STATUS_UNAVAILABLE
    signal: TelemetrySignal


_Outcome = _Read | _Tripped | _Unavailable


def _whole_word_pattern(keywords: frozenset[str]) -> re.Pattern[str] | None:
    """Match any keyword as a whole word or phrase; None when there are no keywords."""
    if not keywords:
        return None
    # \b treats "_" as a word character and "-" as a boundary, so "auth" matches
    # "auth-service" but not "author" or "my_auth_helper".
    alternatives = "|".join(re.escape(kw.lower()) for kw in sorted(keywords))
    return re.compile(rf"\b(?:{alternatives})\b")


class TelemetryProbe:
    """Reads Telemetry sources concurrently, each through breaker -> deadline -> retry."""

    def __init__(
        self,
        sources: Sequence[TelemetrySource],
        *,
        breakers: CircuitBreakerRegistry,
        fail_max: int | None = None,
        reset_timeout: int | None = None,
        source_deadline_s: float | None = None,
    ) -> None:
        # A policy value left unset is read from the global settings at construction.
        self._registry = breakers
        self._sources = list(sources)
        self._source_deadline_s = (
            source_deadline_s
            if source_deadline_s is not None
            else settings.telemetry_source_deadline_s
        )
        self._source_breakers: dict[str, AsyncCircuitBreaker] = {
            source.id: breakers.get_or_create(
                source.id,
                fail_max=fail_max if fail_max is not None else settings.circuit_breaker_fail_max,
                reset_timeout=(
                    reset_timeout
                    if reset_timeout is not None
                    else settings.circuit_breaker_reset_timeout
                ),
            )
            for source in self._sources
        }
        self._keyword_patterns = {
            source.id: _whole_word_pattern(source.keywords) for source in self._sources
        }

    @property
    def source_ids(self) -> list[str]:
        """Every source id, in registration order."""
        return [source.id for source in self._sources]

    def describe(self, source_id: str) -> str:
        """Name a source as "<display name> [<id>]"; an id no source owns is named as itself."""
        for source in self._sources:
            if source.id == source_id:
                return f"{source.display_name} [{source.id}]"
        return source_id

    def select_targets(self, query: str, scope: str | None) -> frozenset[str]:
        """Targeted sources: those whose keywords appear in query or scope, else every source."""
        text = f"{query} {scope or ''}".lower()
        matched = frozenset(
            source_id
            for source_id, pattern in self._keyword_patterns.items()
            if pattern is not None and pattern.search(text) is not None
        )
        return matched or frozenset(self.source_ids)

    async def collect(
        self,
        targets: frozenset[str],
        query: str,
        scope: str | None,
        *,
        trace: TraceRecorder | None = None,
        phase: ReadPhase | None = None,
    ) -> TelemetryReading:
        """Read every targeted source, recording each on `trace`; raises only CancelledError."""
        # Each source read is one `telemetry_read` (ADR-008), marked with phase ("setup" for a
        # scenario's pre-run read); with no trace, nothing is recorded.
        targeted = [source for source in self._sources if source.id in targets]
        recorder = trace or NullTrace()
        outcomes = await asyncio.gather(
            *(self._traced_read(s, query, scope, recorder, phase) for s in targeted)
        )
        signals: list[TelemetrySignal] = []
        tripped: set[str] = set()
        unavailable: set[str] = set()
        for source, outcome in zip(targeted, outcomes, strict=True):
            if isinstance(outcome, _Read):
                signals.extend(outcome.signals)
                continue
            (tripped if isinstance(outcome, _Tripped) else unavailable).add(source.id)
            signals.append(outcome.signal)
        return TelemetryReading(
            signals=signals,
            tripped_breakers=frozenset(tripped),
            unavailable_sources=frozenset(unavailable),
            breaker_states=self._breaker_states(),
        )

    async def call_source(self, source_type: type[S], call: Callable[[S], Awaitable[T]]) -> T:
        """Run call(source) on the first source of source_type under its guard; raises."""
        source: S = next(s for s in self._sources if isinstance(s, source_type))
        return await self._guarded(source.id, lambda: call(source))

    async def _guarded(self, source_id: str, read: Callable[[], Awaitable[T]]) -> T:
        """Run read() through the source's breaker, deadline and retry; raises on failure."""
        retrying = with_retry()(read)

        async def within_deadline() -> T:
            async with asyncio.timeout(self._source_deadline_s):
                return await retrying()

        return await self._source_breakers[source_id].call(within_deadline)

    async def _traced_read(
        self,
        source: TelemetrySource,
        query: str,
        scope: str | None,
        trace: TraceRecorder,
        phase: ReadPhase | None,
    ) -> _Outcome:
        """Read one source, recording a telemetry_read: attempts, outcome and breaker states."""
        breaker = self._source_breakers[source.id]
        before = breaker.state
        attempts = 0

        async def read_once() -> list[TelemetrySignal]:
            # Counted here, so a refused read is 0 attempts and each retry is one more.
            nonlocal attempts
            attempts += 1
            return await source.read(query, scope)

        started = time.monotonic()
        outcome = await self._read(source, query, scope, read_once)
        duration_ms = ms_since(started)
        read_outcome: ReadOutcome
        match outcome:
            case _Read():
                read_outcome, signals = "read", len(outcome.signals)
            case _Tripped():
                read_outcome, signals = "tripped", 1
            case _Unavailable():
                read_outcome, signals = "unavailable", 1
        after = breaker.state
        record_event(
            trace,
            lambda: TelemetryRead(
                source=source.id,
                phase=phase,
                outcome=read_outcome,
                attempts=attempts,
                breaker_before=_breaker_state(before),
                breaker_after=_breaker_state(after),
                signals=signals,
                duration_ms=duration_ms,
            ),
        )
        return outcome

    async def _read(
        self,
        source: TelemetrySource,
        query: str,
        scope: str | None,
        read: Callable[[], Awaitable[list[TelemetrySignal]]],
    ) -> _Outcome:
        """Run read() under the source's guard and classify it; only CancelledError escapes."""
        started = time.monotonic()
        failure: type[_Tripped | _Unavailable]
        try:
            return _Read(await self._guarded(source.id, read))
        except BreakerOpen as exc:
            logger.warning("Telemetry source %s refused by its breaker: %s", source.id, exc)
            failure = _Tripped
            reason = f"Circuit breaker '{source.id}' tripped OPEN; failing fast without hanging."
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            logger.error("Telemetry source %s read failed: %s", source.id, detail)
            if self._source_breakers[source.id].info().is_open:
                failure = _Tripped
                reason = (
                    f"Circuit breaker '{source.id}' tripped OPEN after this read failed "
                    f"({detail}); failing fast without hanging."
                )
            else:
                failure = _Unavailable
                reason = f"Telemetry source '{source.id}' unavailable: {detail}"
        return failure(self._degraded(source, query, scope, started, failure.STATUS, reason))

    def _degraded(
        self,
        source: TelemetrySource,
        query: str,
        scope: str | None,
        started: float,
        status: str,
        reason: str,
    ) -> TelemetrySignal:
        """Build the one degraded signal a failed source contributes."""
        try:
            label = source.degraded_label(query, scope)
        except Exception as exc:
            logger.warning("Telemetry source %s degraded_label failed: %s", source.id, exc)
            label = source.id
        return TelemetrySignal(
            source=label,
            status=status,
            latency_ms=round((time.monotonic() - started) * 1000.0, 2),
            error_rate=1.0,
            raw_snippet=reason,
        )

    def _breaker_states(self) -> dict[str, str]:
        """Every registered breaker's state; empty if the registry cannot be read."""
        try:
            return {name: info.state for name, info in self._registry.get_all_statuses().items()}
        except Exception as exc:
            logger.warning("Reading breaker statuses failed: %s", exc)
            return {}


def _breaker_state(state: str) -> BreakerState:
    """Narrow a breaker's state name to a trace BreakerState; raises on an unknown one."""
    match state:
        case "closed":
            return "closed"
        case "open":
            return "open"
        case "half-open":
            return "half-open"
    raise ValueError(f"Unknown breaker state: {state}")
