"""Tests for StatuspageClient, ChaosManager, and ChaosClient with resilience guards."""

import pytest
import httpx
from unittest.mock import AsyncMock

from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry
from resilient_triage.schemas.telemetry import ChaosConfig
from resilient_triage.telemetry.chaos import (
    ChaosClient,
    ChaosManager,
    resolve_probe_source,
)
from resilient_triage.telemetry.statuspage import StatuspageClient


@pytest.mark.asyncio
async def test_statuspage_client_parsing_and_signals():
    """Verify StatuspageClient parses API payloads and produces TelemetrySignal records."""
    mock_payload = {
        "page": {"name": "GitHub", "url": "https://www.githubstatus.com"},
        "status": {"indicator": "major", "description": "Major Service Outage"},
        "components": [
            {"id": "c1", "name": "Git Operations", "status": "operational"},
            {"id": "c2", "name": "Actions", "status": "major_outage", "description": "Actions unavailable"},
        ],
        "incidents": [
            {
                "id": "inc1",
                "name": "Degraded Actions execution",
                "status": "investigating",
                "impact": "critical",
                "shortlink": "https://stspg.io/xyz",
            }
        ],
    }

    mock_resp = httpx.Response(
        200,
        request=httpx.Request("GET", "https://statuspage.test/summary.json"),
        json=mock_payload,
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_resp

    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("test_statuspage_cb", fail_max=3)

    sp_client = StatuspageClient(
        url="https://statuspage.test/summary.json",
        circuit_breaker=cb,
        client=mock_client,
    )

    signals = await sp_client.fetch_signals()
    assert len(signals) == 3

    global_signal = next(s for s in signals if s.source == "statuspage:global")
    assert global_signal.status == "major"
    assert "GitHub" in (global_signal.raw_snippet or "")

    component_signal = next(s for s in signals if "Actions" in s.source)
    assert component_signal.status == "major_outage"
    assert component_signal.error_rate == 0.8

    incident_signal = next(s for s in signals if "incident" in s.source)
    assert incident_signal.status == "investigating"
    assert incident_signal.error_rate == 1.0


@pytest.mark.asyncio
async def test_statuspage_client_circuit_breaker_fallback():
    """Verify StatuspageClient returns degraded signal when circuit breaker is tripped."""
    mock_resp = httpx.Response(
        500,
        request=httpx.Request("GET", "https://statuspage.test/summary.json"),
        content=b"Internal Server Error",
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_resp

    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("test_statuspage_failing", fail_max=3)

    sp_client = StatuspageClient(
        url="https://statuspage.test/summary.json",
        circuit_breaker=cb,
        client=mock_client,
    )

    # Trigger 3 failed operations to trip breaker open
    for _ in range(3):
        signals = await sp_client.fetch_signals()
        assert len(signals) == 1

    assert cb.is_open is True

    # 4th call should immediately return degraded signal with CIRCUIT_OPEN
    fallback_signals = await sp_client.fetch_signals()
    assert len(fallback_signals) == 1
    assert fallback_signals[0].status == "CIRCUIT_OPEN"
    assert "tripped OPEN" in (fallback_signals[0].raw_snippet or "")


@pytest.mark.asyncio
async def test_chaos_manager_and_client_trip():
    """Verify ChaosManager runtime updates and ChaosClient circuit breaker tripping."""
    mgr = ChaosManager()
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("test_chaos_cb", fail_max=3)
    client = ChaosClient(manager=mgr, circuit_breaker=cb)

    # 1. Healthy state
    mgr.update_config(ChaosConfig(active=False))
    signals = await client.fetch_signals()
    assert len(signals) == 1
    assert signals[0].status == "OK"
    assert cb.current_state == "closed"

    # 2. Inject 503 chaos
    mgr.update_config(ChaosConfig(active=True, inject_503=True))

    # 3 consecutive failed operations trip the breaker
    for i in range(3):
        signals = await client.fetch_signals()
        assert len(signals) == 1
        if i < 2:
            assert signals[0].status in {"UNAVAILABLE", "DEGRADED"}
        else:
            # On the 3rd operation, threshold reached and breaker trips
            assert signals[0].status in {"CIRCUIT_OPEN", "UNAVAILABLE"}

    assert cb.is_open is True

    # 4th call fails fast with CIRCUIT_OPEN
    tripped_signals = await client.fetch_signals()
    assert len(tripped_signals) == 1
    assert tripped_signals[0].status == "CIRCUIT_OPEN"
    assert tripped_signals[0].latency_ms < 10.0  # Fast fail without hanging


@pytest.mark.asyncio
async def test_statuspage_client_short_ttl_cache():
    """Verify StatuspageClient short-TTL cache prevents redundant network fetches."""
    from resilient_triage.telemetry.statuspage import clear_statuspage_cache

    clear_statuspage_cache()
    payload = {
        "page": {"id": "test_page", "name": "CacheTest Status", "url": "https://status.test"},
        "status": {"indicator": "none", "description": "All Systems Operational"},
        "components": [],
        "incidents": [],
    }
    mock_resp = httpx.Response(
        200,
        request=httpx.Request("GET", "https://cache.test/summary.json"),
        json=payload,
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_resp

    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("test_statuspage_cache_cb")

    sp_client = StatuspageClient(
        url="https://cache.test/summary.json",
        circuit_breaker=cb,
        client=mock_client,
        cache_ttl_seconds=10.0,
    )

    # First fetch: calls client.get
    summary1 = await sp_client.fetch_summary()
    assert summary1.page_name == "CacheTest Status"
    assert mock_client.get.call_count == 1

    # Second fetch within 10s: serves from cache, no network call
    summary2 = await sp_client.fetch_summary()
    assert summary2.page_name == "CacheTest Status"
    assert mock_client.get.call_count == 1

    clear_statuspage_cache()


@pytest.mark.asyncio
async def test_chaos_latency_injection():
    """Verify that configuring latency_ms introduces actual artificial execution delay."""
    import time

    mgr = ChaosManager()
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("test_chaos_latency_cb", fail_max=5)
    client = ChaosClient(manager=mgr, circuit_breaker=cb)

    mgr.update_config(ChaosConfig(active=True, latency_ms=80, inject_503=False, failure_rate=0.0))

    start = time.monotonic()
    resp = await client.probe()
    elapsed_ms = (time.monotonic() - start) * 1000.0

    assert resp.status == "OK"
    assert resp.latency_injected_ms == 80
    assert elapsed_ms >= 70.0  # At least 70ms with scheduling margin


@pytest.mark.asyncio
async def test_chaos_failure_rate_probabilistic():
    """Verify that failure_rate = 1.0 triggers 503 fault via the probabilistic path."""
    mgr = ChaosManager()
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("test_chaos_failure_rate_cb", fail_max=5)
    client = ChaosClient(manager=mgr, circuit_breaker=cb)

    # Injected 503 is false, but failure rate is 100%
    mgr.update_config(ChaosConfig(active=True, inject_503=False, failure_rate=1.0))

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.probe()

    assert exc_info.value.response.status_code == 503


@pytest.mark.asyncio
async def test_chaos_inactive_ignores_faults():
    """Verify that when active is False, faults (latency, 503, failure_rate) are not injected."""
    import time

    mgr = ChaosManager()
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("test_chaos_inactive_cb", fail_max=5)
    client = ChaosClient(manager=mgr, circuit_breaker=cb)

    # Active is False even with latency and 503 set
    mgr.update_config(ChaosConfig(active=False, latency_ms=500, inject_503=True, failure_rate=1.0))

    start = time.monotonic()
    resp = await client.probe()
    elapsed_ms = (time.monotonic() - start) * 1000.0

    assert resp.status == "OK"
    assert resp.latency_injected_ms == 0
    assert elapsed_ms < 50.0  # Runs immediately without delay


def test_resolve_probe_source():
    """Verify resolve_probe_source dynamically maps subsystem hints to domain probes."""
    assert resolve_probe_source(service_hint="api_gateway") == "api_gateway_probe"
    assert resolve_probe_source(query="Ingress router 502 errors") == "api_gateway_probe"
    assert resolve_probe_source(service_hint="postgres") == "postgres_health_probe"
    assert resolve_probe_source(query="Database connection pool exhaustion") == "postgres_health_probe"
    assert resolve_probe_source(service_hint="payment_service") == "payment_service_probe"
    assert resolve_probe_source(query="Checkout billing webhook timeout") == "payment_service_probe"
    assert resolve_probe_source(service_hint="auth-service") == "auth_service_probe"
    assert resolve_probe_source(query="JWT session token validation latency") == "auth_service_probe"
    assert resolve_probe_source(service_hint="unknown_worker", query="general alert") == "cluster_health_probe"
    assert resolve_probe_source() == "cluster_health_probe"


@pytest.mark.asyncio
async def test_chaos_fetch_signals_dynamic_attribution():
    """Verify fetch_signals uses context-aware probe naming and returns authentic error symptoms."""
    mgr = ChaosManager()
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("test_dynamic_attribution_cb", fail_max=5)
    client = ChaosClient(manager=mgr, circuit_breaker=cb)

    # 1. Healthy fetch with DB hint
    signals = await client.fetch_signals(service_hint="postgres", query="Pool timeout")
    assert len(signals) == 1
    assert signals[0].source == "postgres_health_probe"
    assert signals[0].status == "OK"

    # 2. Inject 503 error - verify symptom contains NO simulator jargon
    mgr.update_config(ChaosConfig(active=True, inject_503=True))
    signals_err = await client.fetch_signals(service_hint="gateway", query="Ingress dropping traffic")
    assert len(signals_err) == 1
    assert signals_err[0].source == "api_gateway_probe"
    assert signals_err[0].status == "UNAVAILABLE"
    # Ensure simulator jargon is eliminated
    assert "Injected Chaos Fault" not in (signals_err[0].raw_snippet or "")
    assert "worker pool saturated" in (signals_err[0].raw_snippet or "")


