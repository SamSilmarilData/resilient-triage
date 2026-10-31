"""Canned Statuspage sources for scenario runs: a fixed summary, or a read that always fails.

Both have the real source's id, name and keywords (`id = "statuspage"`), so the probe, its
breaker and the graph treat them exactly as the live source; neither makes an HTTP call
(ADR-009).
"""

import time

import httpx

from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.telemetry import StatuspageSummary
from resilient_triage.telemetry.statuspage import (
    GLOBAL_SIGNAL_SOURCE,
    StatuspageSource,
    summary_signals,
)


class CannedStatuspageSource:
    """Statuspage source that reads a fixed summary."""

    id = StatuspageSource.id
    display_name = StatuspageSource.display_name
    keywords = StatuspageSource.keywords

    def __init__(self, summary: StatuspageSummary) -> None:
        self._summary = summary

    def degraded_label(self, query: str, scope: str | None) -> str:
        """Signal source used when this source cannot be read."""
        return GLOBAL_SIGNAL_SOURCE

    async def read(self, query: str, scope: str | None) -> list[TelemetrySignal]:
        """Parse the fixed summary into signals, as the live source parses a fetched one."""
        started = time.monotonic()
        return summary_signals(self._summary, round((time.monotonic() - started) * 1000.0, 2))


class FailingStatuspageSource:
    """Statuspage source whose every read fails to connect: the scenario's unavailable source."""

    id = StatuspageSource.id
    display_name = StatuspageSource.display_name
    keywords = StatuspageSource.keywords

    def degraded_label(self, query: str, scope: str | None) -> str:
        """Signal source used when this source cannot be read."""
        return GLOBAL_SIGNAL_SOURCE

    async def read(self, query: str, scope: str | None) -> list[TelemetrySignal]:
        """Raise a connection error, which the probe retries as a transient failure."""
        raise httpx.ConnectError("Statuspage unreachable (scenario fault)")
