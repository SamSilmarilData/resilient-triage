"""Tests for StatuspageClient, ChaosManager, and ChaosClient with resilience guards."""

import pytest
import httpx
from unittest.mock import AsyncMock

from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry
from resilient_triage.schemas.telemetry import ChaosConfig
from resilient_triage.telemetry.chaos import ChaosClient, ChaosManager
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
