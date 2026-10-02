"""End-to-end integration and resilience test suite for FastAPI gateway."""

import pytest
import pytest_asyncio
import httpx
from unittest.mock import patch

from resilient_triage.api.app import app
from resilient_triage.cache.semantic_cache import semantic_cache
from resilient_triage.graph.llm import MockTriageChatModel, set_triage_llm
from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
from resilient_triage.telemetry.chaos import chaos_manager


@pytest_asyncio.fixture(autouse=True)
async def reset_api_environment():
    """Reset circuit breakers, chaos manager, cache, and LLM override between tests."""
    circuit_breaker_registry.reset_all()
    chaos_manager.reset()
    await semantic_cache.clear()
    set_triage_llm(MockTriageChatModel())
    yield
    circuit_breaker_registry.reset_all()
    chaos_manager.reset()
    await semantic_cache.clear()
    set_triage_llm(None)


@pytest_asyncio.fixture
async def client():
    """Yield an async test client configured with app lifespan."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as ac:
            yield ac


@pytest.mark.asyncio
async def test_health_endpoint(client: httpx.AsyncClient):
    """Verify /health returns 200, system version, and circuit breaker states."""
    resp = await client.get("/health")
    assert resp.status_code == 200

    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == "1.1.0"
    assert "statuspage" in data["circuits"]
    assert "chaos" in data["circuits"]
    assert data["circuits"]["statuspage"] == "closed"
    assert data["circuits"]["chaos"] == "closed"

    # Middleware headers
    assert "x-request-id" in resp.headers
    assert "x-process-time-ms" in resp.headers


@pytest.mark.asyncio
async def test_triage_cache_miss_and_headers(client: httpx.AsyncClient):
    """Verify first query results in X-Cache: MISS and valid triage report."""
    payload = {
        "query": "GitHub Actions queue is stalling with 504 gateway timeout",
        "service_filter": "actions",
    }
    resp = await client.post("/triage", json=payload)
    assert resp.status_code == 200

    assert resp.headers.get("x-cache") == "MISS"
    assert "x-thread-id" in resp.headers
    assert "x-request-id" in resp.headers

    data = resp.json()
    assert data["cached"] is False
    assert data["cache_similarity"] is None
    assert data["execution_time_ms"] > 0

    report = data["report"]
    assert report["summary"] is not None
    assert report["severity"] in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"]
    assert len(report["recommended_actions"]) > 0


@pytest.mark.asyncio
async def test_triage_semantic_cache_hit_under_20ms(client: httpx.AsyncClient):
    """Verify semantically similar query hits cache in sub-20ms with X-Cache: HIT."""
    q1 = "GitHub Actions workflows failing with 503 service unavailable"
    q2 = "GitHub Actions jobs are failing with 503 service unavailable errors"

    # 1. First query: cache miss
    resp1 = await client.post("/triage", json={"query": q1})
    assert resp1.status_code == 200
    assert resp1.headers.get("x-cache") == "MISS"

    # 2. Second query: semantic match
    resp2 = await client.post("/triage", json={"query": q2})
    assert resp2.status_code == 200

    assert resp2.headers.get("x-cache") == "HIT"
    similarity = float(resp2.headers.get("x-cache-similarity", "0.0"))
    assert similarity >= 0.85  # strong semantic match

    data2 = resp2.json()
    assert data2["cached"] is True
    assert data2["cache_similarity"] >= 0.85
    # Strict sub-20ms validation for semantic cache hit
    assert data2["execution_time_ms"] < 20.0


@pytest.mark.asyncio
async def test_triage_force_refresh_bypasses_cache(client: httpx.AsyncClient):
    """Verify force_refresh=True skips cache lookup even when an entry exists."""
    q = "Database connection pool exhausted in billing service"

    # Populate cache
    resp1 = await client.post("/triage", json={"query": q})
    assert resp1.headers.get("x-cache") == "MISS"

    # Subsequent request with force_refresh=True
    resp2 = await client.post("/triage", json={"query": q, "force_refresh": True})
    assert resp2.status_code == 200
    assert resp2.headers.get("x-cache") == "MISS"
    assert resp2.json()["cached"] is False


@pytest.mark.asyncio
async def test_triage_thread_id_checkpointing(client: httpx.AsyncClient):
    """Verify client-specified X-Thread-ID is preserved across triage turns."""
    custom_thread_id = "test-session-thread-999"
    resp = await client.post(
        "/triage",
        json={"query": "High CPU utilization detected on worker nodes"},
        headers={"X-Thread-ID": custom_thread_id},
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-thread-id") == custom_thread_id


@pytest.mark.asyncio
async def test_chaos_injection_and_config(client: httpx.AsyncClient):
    """Verify /chaos/inject dynamically modifies runtime chaos parameters."""
    trigger_payload = {
        "active": True,
        "inject_503": True,
        "failure_rate": 0.75,
        "latency_ms": 25,
    }
    resp = await client.post("/chaos/inject", json=trigger_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is True
    assert data["inject_503"] is True
    assert data["failure_rate"] == 0.75
    assert data["latency_ms"] == 25

    # Verify GET /chaos/config reflects updated state
    cfg_resp = await client.get("/chaos/config")
    assert cfg_resp.status_code == 200
    assert cfg_resp.json() == data


@pytest.mark.asyncio
async def test_chaos_probe_trips_circuit_breaker(client: httpx.AsyncClient):
    """Verify injected 503 causes chaos probe to trip circuit breaker into OPEN state."""
    # 1. Enable 503 chaos
    await client.post("/chaos/inject", json={"active": True, "inject_503": True})

    chaos_cb = circuit_breaker_registry.get_or_create("chaos")
    assert chaos_cb.is_open is False

    # 2. Probe until fail_max is reached and breaker trips
    for _ in range(chaos_cb.fail_max):
        try:
            await client.get("/chaos/probe")
        except Exception:
            pass

    assert chaos_cb.is_open is True

    # 3. /health should now report degraded status
    health_resp = await client.get("/health")
    assert health_resp.status_code == 200
    health_data = health_resp.json()
    assert health_data["status"] == "degraded"
    assert health_data["circuits"]["chaos"] == "open"


@pytest.mark.asyncio
async def test_end_to_end_resilience_under_chaos(client: httpx.AsyncClient):
    """Verify system degrades gracefully and delivers report when circuit breaker trips."""
    # 1. Force chaos circuit breaker to trip OPEN
    chaos_cb = circuit_breaker_registry.get_or_create("chaos")
    for _ in range(chaos_cb.fail_max):
        with pytest.raises(Exception):
            await chaos_cb.call(httpx.AsyncClient().get, "http://invalid-url-to-fail")
    assert chaos_cb.is_open is True

    # 2. Trigger triage request through API
    resp = await client.post(
        "/triage",
        json={"query": "Cluster ingress returning 502 bad gateway across pods"},
    )
    assert resp.status_code == 200

    report = resp.json()["report"]
    # The LangGraph compensatory degraded branch must record the degradation
    assert report["degradation_status"] != "HEALTHY"
    assert "chaos" in report["circuit_breakers_tripped"]
    assert len(report["recommended_actions"]) > 0


@pytest.mark.asyncio
async def test_resilience_status_and_reset(client: httpx.AsyncClient):
    """Verify /resilience/status diagnostics and /resilience/reset recovery."""
    # Trip chaos breaker
    chaos_cb = circuit_breaker_registry.get_or_create("chaos")
    for _ in range(chaos_cb.fail_max):
        with pytest.raises(Exception):
            await chaos_cb.call(httpx.AsyncClient().get, "http://invalid-url-to-fail")

    # Inspect resilience status
    status_resp = await client.get("/resilience/status")
    assert status_resp.status_code == 200
    diag = status_resp.json()
    assert "circuits" in diag
    assert diag["circuits"]["chaos"]["is_open"] is True
    assert diag["circuits"]["chaos"]["state"] == "open"

    # Reset resilience
    reset_resp = await client.post("/resilience/reset")
    assert reset_resp.status_code == 200

    # Verify breaker is closed and health restored
    assert chaos_cb.is_open is False
    health_resp = await client.get("/health")
    assert health_resp.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_cache_clear_endpoint(client: httpx.AsyncClient):
    """Verify /cache/clear flushes cached entries."""
    q = "Latency spike observed in authentication microservice"

    # Prime cache
    await client.post("/triage", json={"query": q})
    # Verify cached
    hit_resp = await client.post("/triage", json={"query": q})
    assert hit_resp.headers.get("x-cache") == "HIT"

    # Clear cache
    clear_resp = await client.post("/cache/clear")
    assert clear_resp.status_code == 200

    # Should be MISS now
    after_clear_resp = await client.post("/triage", json={"query": q})
    assert after_clear_resp.headers.get("x-cache") == "MISS"


@pytest.mark.asyncio
async def test_global_exception_handler_sanitization(client: httpx.AsyncClient):
    """Verify unhandled exceptions return structured JSON without leaking tracebacks."""
    with patch("resilient_triage.api.app.triage_graph.ainvoke", side_effect=RuntimeError("Secret internal db failure")):
        resp = await client.post(
            "/triage",
            json={"query": "Trigger unhandled exception failure", "force_refresh": True},
        )
        assert resp.status_code == 500
        data = resp.json()
        assert data["error"] == "InternalServerError"
        assert data["message"] == "An unexpected error occurred during incident triage."
        assert "request_id" in data
        assert "Secret internal db failure" not in str(data)
        assert resp.headers.get("x-request-id") == data["request_id"]
