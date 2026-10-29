"""Atlassian Statuspage consumer guarded by circuit breaker and Tenacity retries."""

import asyncio
import logging
import time

import httpx

from resilient_triage.config import settings
from resilient_triage.resilience.circuit_breaker import (
    BreakerOpen,
    CircuitBreakerRegistry,
)
from resilient_triage.resilience.retry import with_retry
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.telemetry import SIGNAL_STATUS_UNAVAILABLE, StatuspageSummary

logger = logging.getLogger(__name__)

# Strict timeouts agreed upon during design interview
STATUSPAGE_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=1.0)

# Short-TTL per-source cache that skips redundant ~700ms fetches.
SUMMARY_CACHE_TTL_SECONDS = 10.0


def new_statuspage_http_client() -> httpx.AsyncClient:
    """Build a keep-alive pooled httpx.AsyncClient for Statuspage (no I/O until used)."""
    return httpx.AsyncClient(
        timeout=STATUSPAGE_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )


class StatuspageClient:
    """Async Atlassian Statuspage consumer guarded against network and upstream failures."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        breaker_registry: CircuitBreakerRegistry,
        url: str | None = None,
        cache_ttl_seconds: float = SUMMARY_CACHE_TTL_SECONDS,
    ) -> None:
        self.url = url or settings.statuspage_url
        self.circuit_breaker = breaker_registry.get_or_create(
            name="statuspage",
            fail_max=settings.circuit_breaker_fail_max,
            reset_timeout=settings.circuit_breaker_reset_timeout,
        )
        self._client = client
        # Maps url -> (StatuspageSummary, timestamp); private to this source.
        self._summary_cache: dict[str, tuple[StatuspageSummary, float]] = {}
        self.cache_ttl_seconds = cache_ttl_seconds

    async def _fetch_raw(self) -> StatuspageSummary:
        """Fetch raw JSON from Statuspage endpoint and parse into summary model with 10s caching."""
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

    async def fetch_summary(self) -> StatuspageSummary:
        """Fetch status summary with Tenacity retries nested inside circuit breaker.

        Returns:
            StatuspageSummary: Validated summary payload model.

        Raises:
            BreakerOpen: If the statuspage circuit breaker is OPEN.
            httpx.HTTPStatusError: If upstream returns 5xx or transient errors after retries.
            TimeoutError: If the fetch, retries included, outruns the source deadline.
        """
        # breaker -> source deadline -> retry -> read; httpx timeouts stay the inner limit.
        retry_fn = with_retry()(self._fetch_raw)

        async def within_deadline() -> StatuspageSummary:
            async with asyncio.timeout(settings.telemetry_source_deadline_s):
                return await retry_fn()

        return await self.circuit_breaker.call(within_deadline)

    async def fetch_signals(self) -> list[TelemetrySignal]:
        """Fetch status signals with graceful degraded fallback on failure or tripped breaker.

        Never throws network exceptions into graph execution; captures failures and returns
        degraded TelemetrySignal entries denoting CIRCUIT_OPEN or UNAVAILABLE status.

        Returns:
            list[TelemetrySignal]: List of telemetry signals gathered from the Statuspage.
        """
        start_time = time.monotonic()
        try:
            summary = await self.fetch_summary()
            elapsed_ms = (time.monotonic() - start_time) * 1000.0

            signals: list[TelemetrySignal] = [
                TelemetrySignal(
                    source="statuspage:global",
                    status=summary.indicator,
                    latency_ms=round(elapsed_ms, 2),
                    error_rate=0.0 if summary.indicator == "none" else 0.5,
                    raw_snippet=f"{summary.page_name}: {summary.description}",
                )
            ]

            # Collect degraded components
            for comp in summary.components:
                if comp.status != "operational":
                    signals.append(
                        TelemetrySignal(
                            source=f"statuspage:component:{comp.name}",
                            status=comp.status,
                            latency_ms=round(elapsed_ms, 2),
                            error_rate=0.8 if "outage" in comp.status else 0.3,
                            raw_snippet=comp.description or f"Component status is {comp.status}",
                        )
                    )

            # Collect active incidents
            for inc in summary.incidents:
                signals.append(
                    TelemetrySignal(
                        source=f"statuspage:incident:{inc.name}",
                        status=inc.status,
                        latency_ms=round(elapsed_ms, 2),
                        error_rate=1.0 if inc.impact == "critical" else 0.6,
                        raw_snippet=f"Impact: {inc.impact}. Details: {inc.shortlink or 'Ongoing'}",
                    )
                )

            return signals

        except BreakerOpen as cb_err:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            logger.warning("Statuspage circuit breaker OPEN: %s", cb_err)
            return [
                TelemetrySignal(
                    source="statuspage:global",
                    status="CIRCUIT_OPEN",
                    latency_ms=round(elapsed_ms, 2),
                    error_rate=1.0,
                    raw_snippet=(
                        f"Circuit breaker '{self.circuit_breaker.name}' tripped OPEN; failing "
                        "fast without hanging."
                    ),
                )
            ]
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            logger.error("Statuspage fetch failed after retries: %s", exc)
            return [
                TelemetrySignal(
                    source="statuspage:global",
                    status=SIGNAL_STATUS_UNAVAILABLE,
                    latency_ms=round(elapsed_ms, 2),
                    error_rate=1.0,
                    raw_snippet=f"Statuspage unavailable: {exc}",
                )
            ]
