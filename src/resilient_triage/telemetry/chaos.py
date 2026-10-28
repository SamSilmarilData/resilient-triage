"""Local configurable chaos probe and simulation manager."""

import asyncio
import logging
import random
import threading
import time

import httpx

from resilient_triage.config import settings
from resilient_triage.resilience.circuit_breaker import (
    AsyncCircuitBreaker,
    BreakerOpen,
    circuit_breaker_registry,
)
from resilient_triage.resilience.retry import with_retry
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.telemetry import (
    SIGNAL_STATUS_UNAVAILABLE,
    ChaosConfig,
    ChaosTelemetryResponse,
)

logger = logging.getLogger(__name__)


class ChaosManager:
    """Thread-safe controller storing runtime chaos configuration."""

    def __init__(self) -> None:
        self._config = ChaosConfig()
        self._lock = threading.Lock()

    def get_config(self) -> ChaosConfig:
        with self._lock:
            return self._config.model_copy()

    def update_config(self, new_config: ChaosConfig) -> ChaosConfig:
        with self._lock:
            self._config = new_config.model_copy()
            logger.info("Chaos configuration updated: %s", self._config.model_dump())
            return self._config.model_copy()

    def reset(self) -> None:
        with self._lock:
            self._config = ChaosConfig()


chaos_manager = ChaosManager()


def resolve_probe_source(service_hint: str | None = None, query: str | None = None) -> str:
    """Dynamically determine probe telemetry identifier matching the queried subsystem."""
    combined = f"{service_hint or ''} {query or ''}".lower()
    if any(k in combined for k in ["gateway", "ingress", "proxy", "router", "alb", "nginx"]):
        return "api_gateway_probe"
    if any(k in combined for k in ["db", "database", "postgres", "sql", "redis", "pool"]):
        return "postgres_health_probe"
    if any(k in combined for k in ["pay", "billing", "checkout", "stripe"]):
        return "payment_service_probe"
    if any(k in combined for k in ["auth", "token", "jwt", "login", "sso", "session"]):
        return "auth_service_probe"
    return "cluster_health_probe"


class ChaosClient:
    """Diagnostic probe client guarded by circuit breaker and Tenacity retries."""

    def __init__(
        self,
        manager: ChaosManager | None = None,
        circuit_breaker: AsyncCircuitBreaker | None = None,
    ) -> None:
        self.manager = manager or chaos_manager
        self.circuit_breaker = circuit_breaker or circuit_breaker_registry.get_or_create(
            name="chaos",
            fail_max=settings.circuit_breaker_fail_max,
            reset_timeout=settings.circuit_breaker_reset_timeout,
        )

    async def _execute_probe(self) -> ChaosTelemetryResponse:
        """Internal probe execution simulating dependencies and evaluating chaos rules."""
        cfg = self.manager.get_config()

        if cfg.active:
            if cfg.latency_ms > 0:
                await asyncio.sleep(cfg.latency_ms / 1000.0)

            should_fail = cfg.inject_503 or (
                cfg.failure_rate > 0.0 and random.random() < cfg.failure_rate
            )

            if should_fail:
                dummy_req = httpx.Request("GET", "http://internal-cluster/probe")
                dummy_resp = httpx.Response(503, request=dummy_req, content=b"Service Unavailable")
                raise httpx.HTTPStatusError(
                    message=(
                        "503 Service Unavailable: upstream connection refused / "
                        "worker pool saturated"
                    ),
                    request=dummy_req,
                    response=dummy_resp,
                )

        details = (
            "Internal cluster service mesh health checks reporting operational status "
            "(HTTP 200). Latency nominal."
        )
        if cfg.active and cfg.latency_ms > 0:
            details = (
                "Internal cluster health check reporting elevated transit delay "
                f"(+{cfg.latency_ms}ms) across service mesh."
            )

        return ChaosTelemetryResponse(
            status="OK",
            latency_injected_ms=cfg.latency_ms if cfg.active else 0,
            error_injected=False,
            details=details,
        )

    async def probe(self) -> ChaosTelemetryResponse:
        """Execute probe wrapped with Tenacity retries inside the circuit breaker.

        Returns:
            ChaosTelemetryResponse: Evaluated probe result with status and latency.

        Raises:
            BreakerOpen: If the chaos circuit breaker is OPEN.
            httpx.HTTPStatusError: If injected 503 or transient failure persists after retries.
        """
        # Tenacity retry sits INSIDE the circuit breaker call
        retry_fn = with_retry()(self._execute_probe)
        return await self.circuit_breaker.call(retry_fn)

    async def fetch_signals(
        self,
        service_hint: str | None = None,
        query: str | None = None,
    ) -> list[TelemetrySignal]:
        """Fetch telemetry signals with safe fallback for tripped circuit breakers.

        Never raises uncaught network errors; catches BreakerOpen or unexpected
        exceptions and returns degraded TelemetrySignal instances to protect the agent.

        Args:
            service_hint: Optional targeted subsystem or service filter.
            query: Optional incident description query for context-aware probe naming.

        Returns:
            list[TelemetrySignal]: Normalized signal observations.
        """
        source_name = resolve_probe_source(service_hint, query)
        start_time = time.monotonic()
        try:
            resp = await self.probe()
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            return [
                TelemetrySignal(
                    source=source_name,
                    status=resp.status,
                    latency_ms=round(elapsed_ms, 2),
                    error_rate=0.0,
                    raw_snippet=resp.details,
                )
            ]
        except BreakerOpen as cb_err:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            logger.warning("Chaos probe circuit breaker OPEN: %s", cb_err)
            return [
                TelemetrySignal(
                    source=source_name,
                    status="CIRCUIT_OPEN",
                    latency_ms=round(elapsed_ms, 2),
                    error_rate=1.0,
                    raw_snippet=(
                        f"Circuit breaker '{self.circuit_breaker.name}' (Cluster Sentinel) "
                        "tripped OPEN: consecutive failures exceeded threshold "
                        f"({self.circuit_breaker.info().fail_max}). "
                        "Failing fast without hanging."
                    ),
                )
            ]
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start_time) * 1000.0
            logger.error("Chaos probe failed after retries: %s", exc)
            return [
                TelemetrySignal(
                    source=source_name,
                    status=SIGNAL_STATUS_UNAVAILABLE,
                    latency_ms=round(elapsed_ms, 2),
                    error_rate=1.0,
                    raw_snippet=f"Cluster health probe check failed: {exc}",
                )
            ]
