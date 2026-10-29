"""Cluster Sentinel: the local diagnostic probe, and the Chaos injection manager it obeys.

The guarded telemetry probe supplies the breaker, source deadline and retry; the
ClusterSentinelSource adapter only runs the probe and names the matching sub-probe.
"""

import asyncio
import logging
import random
import threading
import time

import httpx

from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.telemetry import ChaosConfig, ChaosTelemetryResponse

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


# Sub-probe name -> substrings of "<scope> <query>" that select it, checked in order.
_SUB_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("api_gateway_probe", ("gateway", "ingress", "proxy", "router", "alb", "nginx")),
    ("postgres_health_probe", ("db", "database", "postgres", "sql", "redis", "pool")),
    ("payment_service_probe", ("pay", "billing", "checkout", "stripe")),
    ("auth_service_probe", ("auth", "token", "jwt", "login", "sso", "session")),
)
_DEFAULT_SUB_PROBE = "cluster_health_probe"


def _sub_probe_name(query: str, scope: str | None) -> str:
    """Name the Cluster Sentinel sub-probe matching the queried subsystem."""
    combined = f"{scope or ''} {query or ''}".lower()
    for name, keywords in _SUB_PROBES:
        if any(k in combined for k in keywords):
            return name
    return _DEFAULT_SUB_PROBE


class ClusterSentinelSource:
    """Cluster Sentinel Telemetry source: one sub-probe signal, subject to Chaos injection."""

    id = "chaos"

    def __init__(self, manager: ChaosManager) -> None:
        self.manager = manager

    def degraded_label(self, query: str, scope: str | None) -> str:
        """Signal source used when this source cannot be read: the matched sub-probe."""
        return _sub_probe_name(query, scope)

    async def read(self, query: str, scope: str | None) -> list[TelemetrySignal]:
        """Run the probe and report it under the matched sub-probe name; raises on failure."""
        start_time = time.monotonic()
        resp = await self.probe()
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        return [
            TelemetrySignal(
                source=_sub_probe_name(query, scope),
                status=resp.status,
                latency_ms=round(elapsed_ms, 2),
                error_rate=0.0,
                raw_snippet=resp.details,
            )
        ]

    async def probe(self) -> ChaosTelemetryResponse:
        """Run one unguarded probe, applying the active Chaos injection; raises on failure."""
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
