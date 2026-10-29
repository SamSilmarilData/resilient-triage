"""Guarded telemetry probe: one breaker, deadline and retry policy over every Telemetry source.

Sources only read and parse; the probe owns everything they share. Each read runs
breaker -> source deadline -> retry -> read, so running out of time is a breaker failure,
and a failed read becomes one degraded signal instead of an exception.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from resilient_triage.config import settings
from resilient_triage.resilience.circuit_breaker import (
    AsyncCircuitBreaker,
    BreakerOpen,
    CircuitBreakerRegistry,
)
from resilient_triage.resilience.retry import with_retry
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.telemetry import SIGNAL_STATUS_CIRCUIT_OPEN, SIGNAL_STATUS_UNAVAILABLE

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TelemetrySource(Protocol):
    """One Telemetry source: reads and parses, raising on failure; the probe guards it."""

    @property
    def id(self) -> str:
        """Source id, also its breaker name."""
        ...

    async def read(self, query: str, scope: str | None) -> list[TelemetrySignal]: ...

    def degraded_label(self, query: str, scope: str | None) -> str: ...


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
    signal: TelemetrySignal


@dataclass(frozen=True)
class _Unavailable:
    signal: TelemetrySignal


_Outcome = _Read | _Tripped | _Unavailable


class TelemetryProbe:
    """Reads Telemetry sources concurrently, each through breaker -> deadline -> retry."""

    def __init__(
        self, sources: Sequence[TelemetrySource], *, breakers: CircuitBreakerRegistry
    ) -> None:
        self.breakers = breakers
        self._sources = list(sources)
        self._breakers: dict[str, AsyncCircuitBreaker] = {
            source.id: breakers.get_or_create(
                source.id,
                fail_max=settings.circuit_breaker_fail_max,
                reset_timeout=settings.circuit_breaker_reset_timeout,
            )
            for source in self._sources
        }

    async def collect(
        self, targets: frozenset[str], query: str, scope: str | None
    ) -> TelemetryReading:
        """Read every targeted source; never raises except CancelledError."""
        targeted = [source for source in self._sources if source.id in targets]
        outcomes = await asyncio.gather(*(self._read(s, query, scope) for s in targeted))
        signals: list[TelemetrySignal] = []
        tripped: set[str] = set()
        unavailable: set[str] = set()
        for source, outcome in zip(targeted, outcomes, strict=True):
            if isinstance(outcome, _Read):
                signals.extend(outcome.signals)
            elif isinstance(outcome, _Tripped):
                tripped.add(source.id)
                signals.append(outcome.signal)
            else:
                unavailable.add(source.id)
                signals.append(outcome.signal)
        return TelemetryReading(
            signals=signals,
            tripped_breakers=frozenset(tripped),
            unavailable_sources=frozenset(unavailable),
            breaker_states=self._breaker_states(),
        )

    async def guarded(self, source_id: str, read: Callable[[], Awaitable[T]]) -> T:
        """Run read() through the source's breaker, deadline and retry; raises on failure."""
        retrying = with_retry()(read)

        async def within_deadline() -> T:
            async with asyncio.timeout(settings.telemetry_source_deadline_s):
                return await retrying()

        return await self._breakers[source_id].call(within_deadline)

    async def _read(self, source: TelemetrySource, query: str, scope: str | None) -> _Outcome:
        """Read one source and classify the outcome; only CancelledError escapes."""
        started = time.monotonic()
        try:
            return _Read(await self.guarded(source.id, lambda: source.read(query, scope)))
        except BreakerOpen as exc:
            logger.warning("Telemetry source %s refused by its breaker: %s", source.id, exc)
            reason = f"Circuit breaker '{source.id}' tripped OPEN; failing fast without hanging."
            return _Tripped(self._degraded(source, query, scope, started, tripped=True, why=reason))
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            logger.error("Telemetry source %s read failed: %s", source.id, detail)
            if self._breakers[source.id].info().is_open:
                reason = (
                    f"Circuit breaker '{source.id}' tripped OPEN after this read failed "
                    f"({detail}); failing fast without hanging."
                )
                return _Tripped(
                    self._degraded(source, query, scope, started, tripped=True, why=reason)
                )
            reason = f"Telemetry source '{source.id}' unavailable: {detail}"
            return _Unavailable(
                self._degraded(source, query, scope, started, tripped=False, why=reason)
            )

    def _degraded(
        self,
        source: TelemetrySource,
        query: str,
        scope: str | None,
        started: float,
        *,
        tripped: bool,
        why: str,
    ) -> TelemetrySignal:
        """Build the one degraded signal a failed source contributes."""
        try:
            label = source.degraded_label(query, scope)
        except Exception as exc:
            logger.warning("Telemetry source %s degraded_label failed: %s", source.id, exc)
            label = source.id
        return TelemetrySignal(
            source=label,
            status=SIGNAL_STATUS_CIRCUIT_OPEN if tripped else SIGNAL_STATUS_UNAVAILABLE,
            latency_ms=round((time.monotonic() - started) * 1000.0, 2),
            error_rate=1.0,
            raw_snippet=why,
        )

    def _breaker_states(self) -> dict[str, str]:
        """Every registered breaker's state; empty if the registry cannot be read."""
        try:
            return {name: info.state for name, info in self.breakers.get_all_statuses().items()}
        except Exception as exc:
            logger.warning("Reading breaker statuses failed: %s", exc)
            return {}
