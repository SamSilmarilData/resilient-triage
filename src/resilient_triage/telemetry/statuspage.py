"""Statuspage Telemetry source: reads and parses an Atlassian Statuspage summary.

The guarded telemetry probe supplies the breaker, source deadline and retry; this adapter
only fetches, caches the summary for a short TTL and turns it into signals.
"""

import logging
import time

import httpx

from resilient_triage.config import settings
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.telemetry import StatuspageSummary

logger = logging.getLogger(__name__)

# Strict timeouts agreed upon during design interview; the inner limit under the source deadline.
STATUSPAGE_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=1.0)

# Short-TTL per-source cache that skips redundant ~700ms fetches.
SUMMARY_CACHE_TTL_SECONDS = 10.0

# Signal source of the page-wide status, also the degraded label when a read fails.
GLOBAL_SIGNAL_SOURCE = "statuspage:global"


def new_statuspage_http_client() -> httpx.AsyncClient:
    """Build a keep-alive pooled httpx.AsyncClient for Statuspage (no I/O until used)."""
    return httpx.AsyncClient(
        timeout=STATUSPAGE_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )


class StatuspageSource:
    """Statuspage Telemetry source: one global signal plus degraded components and incidents."""

    id = "statuspage"
    display_name = "Statuspage"
    keywords = frozenset(
        {"github", "statuspage", "actions", "repo", "webhook", "cloud", "aws", "gcp", "atlassian"}
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        url: str | None = None,
        cache_ttl_seconds: float = SUMMARY_CACHE_TTL_SECONDS,
    ) -> None:
        self.url = url or settings.statuspage_url
        self._client = client
        # Maps url -> (StatuspageSummary, timestamp); private to this source.
        self._summary_cache: dict[str, tuple[StatuspageSummary, float]] = {}
        self.cache_ttl_seconds = cache_ttl_seconds

    def degraded_label(self, query: str, scope: str | None) -> str:
        """Signal source used when this source cannot be read."""
        return GLOBAL_SIGNAL_SOURCE

    async def read(self, query: str, scope: str | None) -> list[TelemetrySignal]:
        """Fetch (or reuse) the summary and parse it into signals; raises on failure."""
        start_time = time.monotonic()
        summary = await self._fetch_summary()
        elapsed_ms = round((time.monotonic() - start_time) * 1000.0, 2)
        return summary_signals(summary, elapsed_ms)

    async def _fetch_summary(self) -> StatuspageSummary:
        """Fetch the summary JSON and parse it, serving a cached copy within the TTL."""
        now = time.monotonic()
        if self.cache_ttl_seconds > 0 and self.url in self._summary_cache:
            cached_summary, cached_ts = self._summary_cache[self.url]
            if (now - cached_ts) < self.cache_ttl_seconds:
                logger.debug(
                    "Serving Statuspage summary from in-memory cache (age=%.2fs)", now - cached_ts
                )
                return cached_summary

        resp = await self._client.get(self.url, timeout=STATUSPAGE_TIMEOUT)
        resp.raise_for_status()
        summary = StatuspageSummary.from_api_response(resp.json())
        if self.cache_ttl_seconds > 0:
            self._summary_cache[self.url] = (summary, now)
        return summary


def summary_signals(summary: StatuspageSummary, elapsed_ms: float) -> list[TelemetrySignal]:
    """Turn a summary into one global signal plus one per degraded component and incident."""
    signals: list[TelemetrySignal] = [
        TelemetrySignal(
            source=GLOBAL_SIGNAL_SOURCE,
            status=summary.indicator,
            latency_ms=elapsed_ms,
            error_rate=0.0 if summary.indicator == "none" else 0.5,
            raw_snippet=f"{summary.page_name}: {summary.description}",
        )
    ]
    for comp in summary.components:
        if comp.status != "operational":
            signals.append(
                TelemetrySignal(
                    source=f"statuspage:component:{comp.name}",
                    status=comp.status,
                    latency_ms=elapsed_ms,
                    error_rate=0.8 if "outage" in comp.status else 0.3,
                    raw_snippet=comp.description or f"Component status is {comp.status}",
                )
            )
    for inc in summary.incidents:
        signals.append(
            TelemetrySignal(
                source=f"statuspage:incident:{inc.name}",
                status=inc.status,
                latency_ms=elapsed_ms,
                error_rate=1.0 if inc.impact == "critical" else 0.6,
                raw_snippet=f"Impact: {inc.impact}. Details: {inc.shortlink or 'Ongoing'}",
            )
        )
    return signals
