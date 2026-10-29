"""Guarded telemetry probe: one breaker, deadline and retry policy over every Telemetry source."""

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry
from resilient_triage.schemas.incident import TelemetrySignal

T = TypeVar("T")


@dataclass(frozen=True)
class TelemetryReading:
    """What one collect() saw: signals plus which targeted sources failed, and breaker states."""

    signals: list[TelemetrySignal]
    tripped_breakers: frozenset[str]
    unavailable_sources: frozenset[str]
    breaker_states: dict[str, str]


class TelemetryProbe:
    """Reads Telemetry sources concurrently, each through breaker -> deadline -> retry."""

    def __init__(self, sources: Iterable[object], *, breakers: CircuitBreakerRegistry) -> None:
        self.breakers = breakers

    async def collect(
        self, targets: frozenset[str], query: str, scope: str | None
    ) -> TelemetryReading:
        """Read every targeted source; never raises except CancelledError."""
        raise NotImplementedError

    async def guarded(self, source_id: str, read: Callable[[], Awaitable[T]]) -> T:
        """Run read() through the source's breaker, deadline and retry; raises on failure."""
        raise NotImplementedError
