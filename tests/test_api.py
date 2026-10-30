"""End-to-end integration and resilience test suite for FastAPI gateway."""

import asyncio
import dataclasses
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fakes import AppServer, fake_statuspage_http
from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from resilient_triage.api.app import create_app
from resilient_triage.config import settings
from resilient_triage.llm import RateLimited, ScriptedAdapter, build_llm_gateway
from resilient_triage.runtime import AppRuntime, build_runtime
from resilient_triage.schemas.incident import SeverityLevel
from resilient_triage.telemetry.chaos import ClusterSentinelSource
from resilient_triage.telemetry.statuspage import StatuspageSource


@pytest.mark.asyncio
async def test_health_endpoint(client: httpx.AsyncClient):
    """Verify /health returns 200, system version, and circuit breaker states."""
    resp = await client.get("/health")
    assert resp.status_code == 200

    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == "2.0.0"
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
async def test_chaos_probe_trips_circuit_breaker(client: httpx.AsyncClient, runtime: AppRuntime):
    """Verify injected 503 causes chaos probe to trip circuit breaker into OPEN state."""
    # 1. Enable 503 chaos
    await client.post("/chaos/inject", json={"active": True, "inject_503": True})

    chaos_cb = runtime.breakers.get_or_create("chaos")
    assert chaos_cb.info().is_open is False

    # 2. Probe until fail_max is reached and breaker trips
    for _ in range(chaos_cb.info().fail_max):
        try:
            await client.get("/chaos/probe")
        except Exception:
            pass

    assert chaos_cb.info().is_open is True

    # 3. /health should now report degraded status
    health_resp = await client.get("/health")
    assert health_resp.status_code == 200
    health_data = health_resp.json()
    assert health_data["status"] == "degraded"
    assert health_data["circuits"]["chaos"] == "open"


@pytest.mark.asyncio
async def test_end_to_end_resilience_under_chaos(client: httpx.AsyncClient, runtime: AppRuntime):
    """Verify system degrades gracefully and delivers report when circuit breaker trips."""
    # 1. Force chaos circuit breaker to trip OPEN
    chaos_cb = runtime.breakers.get_or_create("chaos")

    async def _failing_task():
        raise RuntimeError("Injected downstream failure to trip breaker")

    for _ in range(chaos_cb.info().fail_max):
        with pytest.raises(Exception):
            await chaos_cb.call(_failing_task)
    assert chaos_cb.info().is_open is True

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
async def test_resilience_status_and_reset(client: httpx.AsyncClient, runtime: AppRuntime):
    """Verify /resilience/status diagnostics and /resilience/reset recovery."""
    # Trip chaos breaker
    chaos_cb = runtime.breakers.get_or_create("chaos")

    async def _failing_task():
        raise RuntimeError("Injected downstream failure to trip breaker")

    for _ in range(chaos_cb.info().fail_max):
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
    assert chaos_cb.info().is_open is False
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
async def test_global_exception_handler_sanitization(
    client: httpx.AsyncClient, runtime: AppRuntime, monkeypatch
):
    """Verify unhandled exceptions return structured JSON without leaking tracebacks."""
    # /triage degrades instead of raising, so trigger the handler from another route.
    monkeypatch.setattr(
        runtime.llm, "snapshot", MagicMock(side_effect=RuntimeError("Secret internal db failure"))
    )
    resp = await client.get("/llm/quota")
    assert resp.status_code == 500
    data = resp.json()
    assert data["error"] == "InternalServerError"
    assert data["message"] == "An unexpected error occurred during incident triage."
    assert "request_id" in data
    assert "Secret internal db failure" not in str(data)
    assert resp.headers.get("x-request-id") == data["request_id"]


@pytest.mark.asyncio
async def test_triage_graph_exception_degrades_to_a_200_fallback(client: httpx.AsyncClient):
    """A graph that raises gives a 200 MISS with a degraded report, not a 500."""
    with patch(
        "resilient_triage.runtime.triage_graph.ainvoke",
        side_effect=RuntimeError("Secret internal db failure"),
    ):
        resp = await client.post(
            "/triage",
            json={"query": "Checkout latency spike across regions", "force_refresh": True},
            headers={"X-Thread-ID": "incident-thread-72"},
        )

    assert resp.status_code == 200
    assert resp.headers.get("x-cache") == "MISS"
    assert resp.headers.get("x-thread-id") == "incident-thread-72"
    data = resp.json()
    assert data["cached"] is False
    assert data["report"]["degradation_status"] == "SEVERELY_DEGRADED"
    assert "Secret internal db failure" not in str(data)


@pytest.mark.asyncio
async def test_triage_llm_failure_degrades_gracefully(
    client: httpx.AsyncClient, runtime: AppRuntime, monkeypatch
):
    """Verify a failed LLM call (e.g. HTTP 429, network error) still returns 200 with a fallback."""
    failure = RateLimited(retry_after_s=None, scope="unknown", detail="Rate limit reached")
    # Enough failures for every model call one complete() may make.
    scripted = build_llm_gateway(settings, ScriptedAdapter([failure] * 3))
    monkeypatch.setattr(runtime, "deps", dataclasses.replace(runtime.deps, llm=scripted))
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
    return patch("resilient_triage.runtime.triage_graph", graph)


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
async def test_emergency_fallback_report_lists_tripped_breakers(
    client: httpx.AsyncClient, runtime: AppRuntime
):
    """The emergency fallback report names a targeted breaker that was open."""
    chaos_cb = runtime.breakers.get_or_create("chaos")

    async def _failing_task():
        raise RuntimeError("Injected downstream failure to trip breaker")

    for _ in range(chaos_cb.info().fail_max):
        with pytest.raises(Exception):
            await chaos_cb.call(_failing_task)
    assert chaos_cb.info().is_open is True

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
async def test_resilience_reset_clears_llm_quota_counters(
    client: httpx.AsyncClient, runtime: AppRuntime
):
    """Verify POST /resilience/reset resets LLM quota tracker metrics."""
    # Simulate some tracked calls: a primary 429 answered by the fallback model
    adapter = runtime.llm.adapter
    assert isinstance(adapter, ScriptedAdapter)
    adapter.script.append(RateLimited(retry_after_s=None, scope="unknown", detail="Rate limit"))
    await runtime.llm.complete([HumanMessage(content="Is checkout down?")])
    snap_before = runtime.llm.snapshot()
    assert snap_before.total_calls == 2
    assert snap_before.rate_limit_hits == 1
    assert snap_before.failover_events == 1

    # Reset via API
    reset_resp = await client.post("/resilience/reset")
    assert reset_resp.status_code == 200

    snap_after = runtime.llm.snapshot()
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
    """Verify POST /chaos/inject with failure_rate=1.0 makes GET /chaos/probe fail with 503."""
    resp = await client.post(
        "/chaos/inject",
        json={"active": True, "failure_rate": 1.0, "inject_503": False, "latency_ms": 0},
    )
    assert resp.status_code == 200

    probe_resp = await client.get("/chaos/probe")
    assert probe_resp.status_code == 503


@pytest.mark.asyncio
async def test_triage_internal_scope_only_targets_chaos(
    client: httpx.AsyncClient, runtime: AppRuntime
):
    """Verify service_filter='internal' targets only chaos telemetry, not external Statuspage."""
    with (
        patch.object(StatuspageSource, "read", new_callable=AsyncMock) as mock_sp,
        patch.object(
            ClusterSentinelSource, "read", autospec=True, side_effect=ClusterSentinelSource.read
        ) as mock_chaos,
    ):
        resp = await client.post(
            "/triage",
            json={
                "query": (
                    "Internal API gateway cluster failing with 503 outage and elevated probe "
                    "latency"
                ),
                "service_filter": "internal",
                "force_refresh": True,
            },
        )
        assert resp.status_code == 200
        # The Statuspage source must never be invoked for internal scope
        mock_sp.assert_not_called()
        # Cluster Sentinel must be invoked for internal scope
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
async def test_metrics_count_triage_hits_and_misses(client: httpx.AsyncClient):
    """Verify /metrics counts each triage outcome under its cache label."""
    query = {"query": "Checkout API returning 503 errors in us-east-1"}
    assert (await client.post("/triage", json=query)).headers["x-cache"] == "MISS"
    assert (await client.post("/triage", json=query)).headers["x-cache"] == "HIT"

    content = (await client.get("/metrics")).text

    assert 'resilient_triage_requests_total{cache="hit"} 1' in content
    assert 'resilient_triage_requests_total{cache="miss"} 1' in content
    assert 'resilient_triage_requests_total{cache="saturated"} 0' in content


class _BlockingAdapter(ScriptedAdapter):
    """A ScriptedAdapter whose calls wait until `release` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, model, messages):
        self.started.set()
        await self.release.wait()
        return await super().call(model, messages)


@pytest.mark.asyncio
async def test_concurrency_semaphore_shedding(serve_app: AppServer):
    """Verify that a request finding every run slot busy past the queue timeout gets a 503."""
    llm = _BlockingAdapter()
    one_slot = settings.model_copy(
        update={"triage_max_concurrent_runs": 1, "triage_queue_timeout_s": 0.02}
    )
    async with serve_app(llm, one_slot) as (_, ac):
        first = asyncio.create_task(
            ac.post("/triage", json={"query": "Checkout API returning 503 errors"})
        )
        await asyncio.wait_for(llm.started.wait(), timeout=5.0)

        resp = await ac.post(
            "/triage",
            json={"query": "Sudden traffic spike causing saturating concurrency"},
        )
        llm.release.set()
        assert (await first).status_code == 200

    assert resp.status_code == 503
    assert "saturated" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_triage_request_payload_bounds_rejection(client: httpx.AsyncClient):
    """Verify TriageRequest rejects oversized queries or bad service_filter characters (422)."""
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


ADMIN_ROUTES = [
    pytest.param("/chaos/inject", {"active": False}, id="chaos-inject"),
    pytest.param("/resilience/reset", None, id="resilience-reset"),
    pytest.param("/cache/clear", None, id="cache-clear"),
]


@pytest.mark.parametrize(("path", "body"), ADMIN_ROUTES)
@pytest.mark.parametrize(
    ("configured_key", "sent_key", "expected_status"),
    [
        pytest.param(None, None, 403, id="unset-key-no-header"),
        pytest.param(None, "anything", 403, id="unset-key-any-header"),
        pytest.param("", "", 403, id="empty-key-empty-header"),
        pytest.param("super_secret_token_42", None, 401, id="missing-header"),
        pytest.param("super_secret_token_42", "wrong", 401, id="wrong-header"),
        pytest.param("super_secret_token_42", "super_secret_token_42", 200, id="right-header"),
    ],
)
async def test_admin_routes_fail_closed(
    anonymous_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, object] | None,
    configured_key: str | None,
    sent_key: str | None,
    expected_status: int,
):
    """Admin routes refuse every caller without ADMIN_API_KEY, and need the right key with one."""
    secret = None if configured_key is None else SecretStr(configured_key)
    monkeypatch.setattr(settings, "admin_api_key", secret)
    headers = {} if sent_key is None else {"X-Admin-Key": sent_key}

    resp = await anonymous_client.post(path, json=body, headers=headers)

    assert resp.status_code == expected_status


async def test_openapi_declares_admin_key_security_scheme(client: httpx.AsyncClient):
    """/docs gets an Authorize button: the X-Admin-Key header is an OpenAPI security scheme."""
    schema = (await client.get("/openapi.json")).json()

    schemes = schema["components"]["securitySchemes"]
    (scheme_name,) = schemes
    scheme = schemes[scheme_name]
    assert (scheme["type"], scheme["in"], scheme["name"]) == ("apiKey", "header", "X-Admin-Key")
    for route in ADMIN_ROUTES:
        path = route.values[0]
        assert schema["paths"][path]["post"]["security"] == [{scheme_name: []}]
    assert "security" not in schema["paths"]["/triage"]["post"]


@pytest.mark.asyncio
async def test_lifespan_closes_telemetry_and_llm_clients(monkeypatch):
    """Verify the app lifespan starts its runtime and closes the Statuspage and Groq clients."""
    groq_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    monkeypatch.setattr("resilient_triage.llm.groq.new_groq_http_client", lambda t: groq_http)
    statuspage_http = fake_statuspage_http()
    rt = build_runtime(
        settings.model_copy(update={"groq_api_key": "test-key"}), statuspage_http=statuspage_http
    )
    app = create_app(lambda: rt)

    async with app.router.lifespan_context(app):
        # Clients open while the app serves
        assert not statuspage_http.is_closed
        assert not groq_http.is_closed
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            health = (await ac.get("/health")).json()
        assert set(health["circuits"]) == {"statuspage", "chaos"}

    # Teardown
    assert statuspage_http.is_closed
    assert groq_http.is_closed


@pytest.mark.asyncio
async def test_status_routes_ping_the_cache_exactly_once(
    client: httpx.AsyncClient, runtime: AppRuntime, monkeypatch
):
    """Verify /health, /resilience/status and /metrics each ping Redis exactly once."""
    pings: list[str] = []
    real_check = runtime.cache.check_connection

    async def counting_check() -> bool:
        pings.append("ping")
        return await real_check()

    monkeypatch.setattr(runtime.cache, "check_connection", counting_check)

    for route in ("/health", "/resilience/status", "/metrics"):
        pings.clear()
        resp = await client.get(route)
        assert resp.status_code == 200
        assert len(pings) == 1, route


@pytest.mark.asyncio
async def test_two_apps_keep_separate_breakers_and_request_metrics(
    client: httpx.AsyncClient, runtime: AppRuntime, serve_app: AppServer
):
    """Verify a second app instance has its own runtime: breakers and counters don't leak."""
    async with serve_app(ScriptedAdapter()) as (_, other):
        await client.post("/chaos/inject", json={"active": True, "inject_503": True})
        for _ in range(runtime.breakers.get_or_create("chaos").info().fail_max):
            await client.get("/chaos/probe")
        await client.post(
            "/triage", json={"query": "Payments API returning 503", "force_refresh": True}
        )

        assert (await client.get("/health")).json()["status"] == "degraded"
        assert (await other.get("/health")).json()["status"] == "healthy"
        assert (
            'resilient_triage_requests_total{cache="miss"} 1' in (await client.get("/metrics")).text
        )
        assert (
            'resilient_triage_requests_total{cache="miss"} 0' in (await other.get("/metrics")).text
        )
