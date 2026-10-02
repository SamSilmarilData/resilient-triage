"""Atlassian Statuspage consumer guarded by circuit breaker and Tenacity retries."""

import logging
import time
from typing import Any

import httpx
from pybreaker import CircuitBreakerError

from resilient_triage.config import settings
from resilient_triage.resilience.circuit_breaker import (
    AsyncCircuitBreaker,
    circuit_breaker_registry,
)
from resilient_triage.resilience.retry import with_retry
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.telemetry import StatuspageSummary

logger = logging.getLogger(__name__)

# Strict timeouts agreed upon during design interview
STATUSPAGE_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=1.0)


class StatuspageClient:
    """Async Atlassian Statuspage consumer guarded against network and upstream failures."""

    def __init__(
        self,
        url: str | None = None,
        circuit_breaker: AsyncCircuitBreaker | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url or settings.statuspage_url
        self.circuit_breaker = circuit_breaker or circuit_breaker_registry.get_or_create(
            name="statuspage",
            fail_max=settings.circuit_breaker_fail_max,
            reset_timeout=settings.circuit_breaker_reset_timeout,
        )
        self._external_client = client

    async def _fetch_raw(self) -> StatuspageSummary:
        """Fetch raw JSON from Statuspage endpoint and parse into summary model."""
        if self._external_client is not None:
            resp = await self._external_client.get(self.url, timeout=STATUSPAGE_TIMEOUT)
            resp.raise_for_status()
            return StatuspageSummary.from_api_response(resp.json())

        async with httpx.AsyncClient(timeout=STATUSPAGE_TIMEOUT) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
            return StatuspageSummary.from_api_response(resp.json())

    async def fetch_summary(self) -> StatuspageSummary:
        """Fetch status summary with Tenacity retries nested inside circuit breaker."""
        retry_fn = with_retry()(self._fetch_raw)
        return await self.circuit_breaker.call(retry_fn)

    async def fetch_signals(self) -> list[TelemetrySignal]:
        """Fetch status signals with graceful degraded fallback on failure or tripped breaker."""
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

        except CircuitBreakerError as cb_err:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            logger.warning("Statuspage circuit breaker OPEN: %s", cb_err)
            return [
                TelemetrySignal(
                    source="statuspage:global",
                    status="CIRCUIT_OPEN",
                    latency_ms=round(elapsed_ms, 2),
                    error_rate=1.0,
                    raw_snippet=f"Circuit breaker '{self.circuit_breaker.name}' tripped OPEN; failing fast without hanging.",
                )
            ]
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            logger.error("Statuspage fetch failed after retries: %s", exc)
            return [
                TelemetrySignal(
                    source="statuspage:global",
                    status="UNAVAILABLE",
                    latency_ms=round(elapsed_ms, 2),
                    error_rate=1.0,
                    raw_snippet=f"Statuspage unavailable: {exc}",
                )
            ]
