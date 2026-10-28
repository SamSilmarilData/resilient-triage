"""End-to-end integration and resilience test suite for FastAPI gateway."""

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from resilient_triage.graph.llm import groq_quota_tracker
from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
from resilient_triage.schemas.incident import SeverityLevel
from resilient_triage.telemetry.chaos import ChaosClient


@pytest.mark.asyncio
async def test_health_endpoint(client: httpx.AsyncClient):
    """Verify /health returns 200, system version, and circuit breaker states."""
    resp = await client.get("/health")
    assert resp.status_code == 200

    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == "1.3.0"
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
    assert report["severity"] in [s.value for s in SeverityLevel]
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

    async def _failing_task():
        raise RuntimeError("Injected downstream failure to trip breaker")

    for _ in range(chaos_cb.fail_max):
        with pytest.raises(Exception):
            await chaos_cb.call(_failing_task)
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

    async def _failing_task():
        raise RuntimeError("Injected downstream failure to trip breaker")

    for _ in range(chaos_cb.fail_max):
        with pytest.raises(Exception):
            await chaos_cb.call(_failing_task)

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
    with patch(
        "resilient_triage.api.app.triage_graph.ainvoke",
        side_effect=RuntimeError("Secret internal db failure"),
    ):
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


@pytest.mark.asyncio
async def test_triage_llm_failure_degrades_gracefully(client: httpx.AsyncClient):
    """Verify that when LLM inference fails (e.g., HTTP 429 or network error), the system returns 200 with a fallback report."""
    with patch("resilient_triage.graph.nodes.generate_report.get_triage_llm") as mock_get_llm:
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "429 Too Many Requests: Rate limit reached",
                request=httpx.Request("POST", "https://api.groq.com"),
                response=httpx.Response(429),
            )
        )
        mock_get_llm.return_value = mock_llm

        resp = await client.post(
            "/triage",
            json={"query": "High memory pressure on payment service", "force_refresh": True},
        )

        assert resp.status_code == 200
        data = resp.json()
        report = data["report"]
        assert report["degradation_status"] == "SEVERELY_DEGRADED"
        assert (
            "429" in report["root_cause_analysis"]
            or "rate limit" in report["root_cause_analysis"].lower()
        )
        assert len(report["recommended_actions"]) > 0


def _graph_without_final_report():
    """Return a patch making the triage graph run but yield no final_report."""
    from resilient_triage.graph.builder import triage_graph

    async def _ainvoke(*args, **kwargs):
        state = await triage_graph.ainvoke(*args, **kwargs)
        return {k: v for k, v in state.items() if k != "final_report"}

    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=_ainvoke)
    return patch("resilient_triage.api.app.triage_graph", graph)


@pytest.mark.asyncio
async def test_emergency_fallback_report_is_severely_degraded(client: httpx.AsyncClient):
    """The emergency fallback report (graph yields no report) is SEVERELY_DEGRADED."""
    with _graph_without_final_report():
        resp = await client.post(
            "/triage",
            json={"query": "Checkout latency spike across regions", "force_refresh": True},
        )

    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["degradation_status"] == "SEVERELY_DEGRADED"


@pytest.mark.asyncio
async def test_emergency_fallback_report_lists_tripped_breakers(client: httpx.AsyncClient):
    """The emergency fallback report names a targeted breaker that was open."""
    chaos_cb = circuit_breaker_registry.get_or_create("chaos")

    async def _failing_task():
        raise RuntimeError("Injected downstream failure to trip breaker")

    for _ in range(chaos_cb.fail_max):
        with pytest.raises(Exception):
            await chaos_cb.call(_failing_task)
    assert chaos_cb.is_open is True

    with _graph_without_final_report():
        resp = await client.post(
            "/triage",
            json={
                "query": "Internal API gateway cluster failing with 503 outage",
                "service_filter": "internal",
                "force_refresh": True,
            },
        )

    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["degradation_status"] == "SEVERELY_DEGRADED"
    assert report["circuit_breakers_tripped"] == ["chaos"]


@pytest.mark.asyncio
async def test_llm_quota_endpoint(client: httpx.AsyncClient):
    """Verify /llm/quota returns 200 with LLMQuotaSnapshot structure."""
    resp = await client.get("/llm/quota")
    assert resp.status_code == 200
    data = resp.json()

    assert data["provider"] == "groq"
    assert "active_model" in data
    assert "fallback_model" in data
    assert "total_calls" in data
    assert "rate_limit_hits" in data
    assert "failover_events" in data
    assert data["total_calls"] == 0
    assert data["rate_limit_hits"] == 0
    assert data["failover_events"] == 0


@pytest.mark.asyncio
async def test_health_and_resilience_status_contain_llm_quota(client: httpx.AsyncClient):
    """Verify both /health and /resilience/status embed active LLM quota telemetry."""
    # Health endpoint
    h_resp = await client.get("/health")
    assert h_resp.status_code == 200
    h_data = h_resp.json()
    assert "llm_quota" in h_data
    assert h_data["llm_quota"]["provider"] == "groq"

    # Resilience status endpoint
    r_resp = await client.get("/resilience/status")
    assert r_resp.status_code == 200
    r_data = r_resp.json()
    assert "llm_quota" in r_data
    assert r_data["llm_quota"]["provider"] == "groq"


@pytest.mark.asyncio
async def test_resilience_reset_clears_llm_quota_counters(client: httpx.AsyncClient):
    """Verify POST /resilience/reset resets LLM quota tracker metrics."""
    # Simulate some tracked calls
    mock_resp = httpx.Response(
        429,
        headers={
            "x-ratelimit-remaining-tokens": "500",
            "x-ratelimit-limit-tokens": "8000",
        },
        request=httpx.Request("POST", "https://api.groq.com"),
    )
    groq_quota_tracker.record_response(mock_resp, model_name="qwen/qwen3.8-27b")
    snap_before = groq_quota_tracker.get_snapshot()
    assert snap_before.total_calls == 1
    assert snap_before.rate_limit_hits == 1

    # Reset via API
    reset_resp = await client.post("/resilience/reset")
    assert reset_resp.status_code == 200

    snap_after = groq_quota_tracker.get_snapshot()
    assert snap_after.total_calls == 0
    assert snap_after.rate_limit_hits == 0
    assert snap_after.failover_events == 0


@pytest.mark.asyncio
async def test_chaos_probe_latency_via_api(client: httpx.AsyncClient):
    """Verify that POST /chaos/inject with latency_ms slows down GET /chaos/probe."""
    # Configure 80ms artificial latency
    resp = await client.post(
        "/chaos/inject",
        json={"active": True, "latency_ms": 80, "inject_503": False, "failure_rate": 0.0},
    )
    assert resp.status_code == 200

    start = time.monotonic()
    probe_resp = await client.get("/chaos/probe")
    elapsed_ms = (time.monotonic() - start) * 1000.0

    assert probe_resp.status_code == 200
    data = probe_resp.json()
    assert data["status"] == "OK"
    assert data["latency_injected_ms"] == 80
    assert elapsed_ms >= 70.0


@pytest.mark.asyncio
async def test_chaos_probe_failure_rate_via_api(client: httpx.AsyncClient):
    """Verify that POST /chaos/inject with failure_rate=1.0 causes GET /chaos/probe to fail with 503."""
    resp = await client.post(
        "/chaos/inject",
        json={"active": True, "failure_rate": 1.0, "inject_503": False, "latency_ms": 0},
    )
    assert resp.status_code == 200

    probe_resp = await client.get("/chaos/probe")
    assert probe_resp.status_code == 503


@pytest.mark.asyncio
async def test_triage_internal_scope_only_targets_chaos(client: httpx.AsyncClient):
    """Verify that triage requests with service_filter='internal' only target internal chaos telemetry and exclude external Statuspage."""
    with (
        patch("resilient_triage.telemetry.statuspage.StatuspageClient.fetch_signals") as mock_sp,
        patch(
            "resilient_triage.telemetry.chaos.ChaosClient.fetch_signals",
            wraps=ChaosClient().fetch_signals,
        ) as mock_chaos,
    ):
        resp = await client.post(
            "/triage",
            json={
                "query": "Internal API gateway cluster failing with 503 outage and elevated probe latency",
                "service_filter": "internal",
                "force_refresh": True,
            },
        )
        assert resp.status_code == 200
        # StatuspageClient must never be invoked for internal scope
        mock_sp.assert_not_called()
        # ChaosClient must be invoked for internal scope
        assert mock_chaos.called


@pytest.mark.asyncio
async def test_sre_console_renders_html(client: httpx.AsyncClient):
    """Verify that GET / serves the interactive SRE command center single-page console."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Resilient Triage" in resp.text
    assert "Live Architecture Pipeline Simulation" in resp.text
    assert "CACHE &amp; VECTOR TIER" in resp.text or "CACHE & VECTOR TIER" in resp.text
    assert "RESILIENT ORCHESTRATION" in resp.text
    assert "Fast-Path Highway" in resp.text


@pytest.mark.asyncio
async def test_prometheus_metrics_endpoint(client: httpx.AsyncClient):
    """Verify that GET /metrics returns standard Prometheus exposition format."""
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    content = resp.text
    assert "resilient_triage_requests_total" in content
    assert "resilient_triage_circuit_breaker_state" in content
    assert "resilient_triage_cache_entries" in content
    assert "resilient_triage_llm_rate_limit_hits_total" in content


@pytest.mark.asyncio
async def test_concurrency_semaphore_shedding(client: httpx.AsyncClient, monkeypatch):
    """Verify that semaphore exhaustion triggers 503 load shedding."""
    # Temporarily set semaphore to 0 to simulate full saturation
    saturated_sem = asyncio.Semaphore(0)
    # api/__init__.py exposes `app` which shadows the submodule name; use sys.modules for module globals
    app_module = sys.modules["resilient_triage.api.app"]
    monkeypatch.setattr(app_module, "GRAPH_SEMAPHORE", saturated_sem)

    # Patch wait_for timeout to 0.02s for fast test execution
    orig_wait_for = asyncio.wait_for

    async def fast_wait_for(fut, timeout):
        return await orig_wait_for(fut, timeout=0.02)

    monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

    resp = await client.post(
        "/triage",
        json={"query": "Sudden traffic spike causing saturating concurrency"},
    )
    assert resp.status_code == 503
    assert "saturated" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_triage_request_payload_bounds_rejection(client: httpx.AsyncClient):
    """Verify TriageRequest rejects oversized queries or invalid service_filter characters with 422."""
    # 1. Query too long (>2000 chars)
    oversized_query = "A" * 2001
    resp_over = await client.post("/triage", json={"query": oversized_query})
    assert resp_over.status_code == 422

    # 2. Query too short (<3 chars)
    resp_under = await client.post("/triage", json={"query": "ab"})
    assert resp_under.status_code == 422

    # 3. Invalid characters in service_filter
    resp_bad_filter = await client.post(
        "/triage",
        json={"query": "Database connection drop", "service_filter": "db;DROP/TABLE*"},
    )
    assert resp_bad_filter.status_code == 422

    # 4. Valid query and clean service_filter succeeds
    resp_ok = await client.post(
        "/triage",
        json={"query": "Database connection drop", "service_filter": "postgres_db-1"},
    )
    assert resp_ok.status_code == 200


@pytest.mark.asyncio
async def test_admin_api_key_protection(client: httpx.AsyncClient, monkeypatch):
    """Verify mutating control-plane endpoints enforce X-Admin-Key authentication when configured."""
    from resilient_triage.config import settings

    # Configure secret admin key
    monkeypatch.setattr(settings, "admin_api_key", "super_secret_token_42")

    # /chaos/inject rejected without key
    r1 = await client.post("/chaos/inject", json={"active": True})
    assert r1.status_code == 401
    assert "unauthorized" in r1.json()["detail"].lower()

    # /chaos/inject rejected with wrong key
    r2 = await client.post("/chaos/inject", json={"active": True}, headers={"X-Admin-Key": "wrong"})
    assert r2.status_code == 401

    # /chaos/inject accepted with valid key
    r3 = await client.post(
        "/chaos/inject", json={"active": False}, headers={"X-Admin-Key": "super_secret_token_42"}
    )
    assert r3.status_code == 200

    # /resilience/reset rejected without key
    r4 = await client.post("/resilience/reset")
    assert r4.status_code == 401

    # /resilience/reset accepted with valid key
    r5 = await client.post("/resilience/reset", headers={"X-Admin-Key": "super_secret_token_42"})
    assert r5.status_code == 200

    # /cache/clear rejected without key
    r6 = await client.post("/cache/clear")
    assert r6.status_code == 401

    # /cache/clear accepted with valid key
    r7 = await client.post("/cache/clear", headers={"X-Admin-Key": "super_secret_token_42"})
    assert r7.status_code == 200


@pytest.mark.asyncio
async def test_lifespan_closes_telemetry_and_llm_clients():
    """Verify close_statuspage_client and close_groq_async_client execute safely without error."""
    from resilient_triage.graph.llm import close_groq_async_client, get_groq_async_client
    from resilient_triage.telemetry.statuspage import (
        _get_statuspage_client,
        close_statuspage_client,
    )

    # Instantiate clients
    c1 = _get_statuspage_client()
    assert not c1.is_closed
    c2 = get_groq_async_client()
    assert not c2.is_closed

    # Teardown
    await close_statuspage_client()
    assert c1.is_closed
    await close_groq_async_client()
    assert c2.is_closed
