"""FastAPI application for Resilient Incident Triage Gateway."""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from resilient_triage import __version__
from resilient_triage.cache.semantic_cache import semantic_cache
from resilient_triage.config import settings
from resilient_triage.graph.builder import triage_graph
from resilient_triage.graph.llm import (
    close_groq_async_client,
    get_active_model_name,
    groq_quota_tracker,
)
from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
from resilient_triage.schemas.api import (
    ChaosTriggerRequest,
    CircuitSnapshot,
    HealthResponse,
    LLMQuotaSnapshot,
    ResilienceStatusResponse,
    TriageRequest,
    TriageResponse,
)
from resilient_triage.schemas.state import TriageState
from resilient_triage.schemas.telemetry import ChaosConfig, ChaosTelemetryResponse
from resilient_triage.telemetry.chaos import ChaosClient, chaos_manager
from resilient_triage.telemetry.statuspage import close_statuspage_client

logger = logging.getLogger(__name__)


async def verify_admin_key(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    """Validate administrative authorization header if admin_api_key is configured."""
    if settings.admin_api_key is not None:
        if not x_admin_key or x_admin_key != settings.admin_api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: invalid or missing X-Admin-Key header.",
            )


# Concurrency boundary: max 10 concurrent graph executions to prevent stampedes
GRAPH_SEMAPHORE = asyncio.Semaphore(10)
TEMPLATES_DIR = Path(__file__).parent / "templates"

# Prometheus metrics tracking counters
_metrics: dict[str, int] = {
    "requests_hit": 0,
    "requests_miss": 0,
    "requests_saturated": 0,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup, cache pre-warming, and graceful shutdown."""
    logger.info("Initializing semantic cache and warming up embedding models...")
    await semantic_cache.initialize()
    try:
        # Pre-warm embedding model weights and 3 quick presets so initial demo requests run in 0.001ms
        presets = [
            "warmup telemetry query",
            "Internal API gateway service failing with 503 outage and elevated probe latency",
            "[scope: internal] Internal API gateway service failing with 503 outage and elevated probe latency",
            "Internal payment processing failing with 503 service unavailable",
            "[scope: internal] Internal payment processing failing with 503 service unavailable",
            "GitHub Actions workflows are failing with 503 and high webhook latency",
            "[scope: actions] GitHub Actions workflows are failing with 503 and high webhook latency",
            "GitHub Actions workflows failing with 503 service unavailable",
            "GitHub Actions jobs are failing with 503 service unavailable errors",
            "Stripe webhook ingestion failing with 504 gateway timeout",
            "[scope: billing] Stripe webhook ingestion failing with 504 gateway timeout",
            "PostgreSQL primary connection pool exhausted under spike",
            "[scope: database] PostgreSQL primary connection pool exhausted under spike",
        ]
        for p in presets:
            semantic_cache.embedding_provider.embed_query(p)
        logger.info("Pre-warmed %d incident presets in embedding LRU cache.", len(presets))
    except Exception as exc:
        logger.warning("Embedding pre-warmup warning: %s", exc)

    # Ensure default circuit breakers are registered
    circuit_breaker_registry.get_or_create("statuspage")
    circuit_breaker_registry.get_or_create("chaos")

    logger.info("Resilient Triage Gateway online and ready for traffic.")
    yield

    logger.info("Shutting down Resilient Triage Gateway...")
    await semantic_cache.close()
    await close_statuspage_client()
    await close_groq_async_client()


app = FastAPI(
    title="Resilient Incident Triage Gateway",
    description="High-availability LangGraph incident triage state machine with Redis semantic caching and circuit breaker telemetry.",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def process_time_and_correlation_middleware(request: Request, call_next):
    """Add correlation request ID, elapsed execution time, and structured error fallback to every response."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    start_time = time.monotonic()

    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception("Unhandled server error [request_id=%s]: %s", request_id, exc)
        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "InternalServerError",
                "message": "An unexpected error occurred during incident triage.",
                "request_id": request_id,
            },
            headers={
                "X-Request-ID": request_id,
                "X-Process-Time-Ms": f"{elapsed_ms:.2f}",
            },
        )

    elapsed_ms = (time.monotonic() - start_time) * 1000.0
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Sanitize unexpected exceptions into structured JSON without leaking tracebacks."""
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    logger.exception("Unhandled server error [request_id=%s]: %s", request_id, exc)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "InternalServerError",
            "message": "An unexpected error occurred during incident triage.",
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


@app.post("/triage", response_model=TriageResponse)
async def triage_incident(payload: TriageRequest, request: Request):
    """Triage an incident through semantic cache (<20ms) or LangGraph state machine.

    Headers returned:
      - X-Cache: 'HIT' or 'MISS'
      - X-Cache-Similarity: Cosine score (if HIT)
      - X-Thread-ID: Conversation memory checkpoint identifier
    """
    thread_id = request.headers.get("X-Thread-ID") or str(uuid.uuid4())
    start_req = time.monotonic()

    # 1. Check Semantic Cache (<20ms fast path)
    if not payload.force_refresh:
        match = await semantic_cache.lookup(
            query=payload.query,
            service_filter=payload.service_filter,
        )
        if match:
            _metrics["requests_hit"] += 1
            elapsed_ms = (time.monotonic() - start_req) * 1000.0
            response_data = TriageResponse(
                report=match.report,
                cached=True,
                cache_similarity=match.similarity,
                execution_time_ms=round(elapsed_ms, 2),
            )
            return JSONResponse(
                content=response_data.model_dump(mode="json"),
                headers={
                    "X-Cache": "HIT",
                    "X-Cache-Similarity": str(match.similarity),
                    "X-Thread-ID": thread_id,
                },
            )

    # 2. Cache MISS: Acquire concurrency semaphore with 5.0s load-shedding boundary
    try:
        await asyncio.wait_for(GRAPH_SEMAPHORE.acquire(), timeout=5.0)
    except TimeoutError:
        _metrics["requests_saturated"] += 1
        logger.warning("Graph concurrency semaphore saturated (limit 10). Shedding load.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Triage state machine is currently saturated. Please retry with exponential backoff.",
        ) from None

    try:
        initial_state = {
            "query": payload.query,
            "incident_query": payload.query,
            "service_filter": payload.service_filter,
        }
        # Isolate per-execution thread to prevent previous query errors from contaminating state
        execution_thread_id = f"{thread_id}_{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": execution_thread_id}}
        state_result = await triage_graph.ainvoke(initial_state, config=config)
    finally:
        GRAPH_SEMAPHORE.release()

    _metrics["requests_miss"] += 1

    final_report = state_result.get("final_report")
    if not final_report:
        from resilient_triage.graph.nodes.fallback_report import fallback_report_node

        logger.warning("Graph produced no final report. Engaging emergency fallback report.")
        fallback_input = dict(state_result)
        if not fallback_input.get("query"):
            fallback_input["query"] = payload.query
        if not fallback_input.get("service_filter"):
            fallback_input["service_filter"] = payload.service_filter
        fallback_state = fallback_report_node(cast(TriageState, fallback_input))
        final_report = fallback_state["final_report"]
        # Persist emergency fallback report to semantic cache
        await semantic_cache.store(
            query=payload.query,
            report=final_report,
            service_filter=payload.service_filter,
        )

    elapsed_ms = (time.monotonic() - start_req) * 1000.0
    response_data = TriageResponse(
        report=final_report,
        cached=False,
        cache_similarity=None,
        execution_time_ms=round(elapsed_ms, 2),
    )
    return JSONResponse(
        content=response_data.model_dump(mode="json"),
        headers={
            "X-Cache": "MISS",
            "X-Thread-ID": thread_id,
        },
    )


@app.post("/chaos/inject", response_model=ChaosConfig, dependencies=[Depends(verify_admin_key)])
async def inject_chaos(payload: ChaosTriggerRequest):
    """Dynamically update runtime chaos injection parameters.

    Args:
        payload: New ChaosTriggerRequest parameters (latency, 503 injection, failure rate).

    Returns:
        ChaosConfig: Updated active configuration snapshot.
    """
    return chaos_manager.update_config(payload)


@app.get("/chaos/config", response_model=ChaosConfig)
async def get_chaos_config():
    """Retrieve active runtime chaos configuration.

    Returns:
        ChaosConfig: Current chaos settings including latency, error toggles, and status.
    """
    return chaos_manager.get_config()


@app.get("/chaos/probe", response_model=ChaosTelemetryResponse)
async def probe_chaos():
    """Execute live diagnostic probe through Tenacity retries and circuit breaker.

    Returns:
        ChaosTelemetryResponse: Diagnostic outcome from the internal chaos service.
    """
    client = ChaosClient()
    try:
        return await client.probe()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Chaos probe injected failure: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Chaos probe failed or circuit open: {exc}",
        ) from exc


@app.get("/", response_class=HTMLResponse)
async def get_sre_console():
    """Serve the interactive SRE Incident Command Center demo UI.

    Returns:
        HTMLResponse: Rendered single-page SRE management console.
    """
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse(
            content="<h1>Resilient Triage API is running. Visit /docs for OpenAPI specs.</h1>"
        )
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


@app.get("/health", response_model=HealthResponse)
async def get_health():
    """Health check endpoint indicating service availability and breaker status.

    Returns:
        HealthResponse: Status indicator, Redis connection state, active driver, and LLM quota.
    """
    statuses = circuit_breaker_registry.get_all_statuses()
    circuit_states = {name: info.state for name, info in statuses.items()}
    any_open = any(info.is_open for info in statuses.values())
    is_redis_online = await semantic_cache.check_connection()

    return HealthResponse(
        status="degraded" if any_open else "healthy",
        version=__version__,
        redis_connected=is_redis_online,
        cache_driver=semantic_cache.driver,
        circuits=circuit_states,
        active_model=get_active_model_name(),
        llm_quota=groq_quota_tracker.get_snapshot(),
    )


@app.get("/resilience/status", response_model=ResilienceStatusResponse)
async def get_resilience_status():
    """Detailed resilience diagnostic report for circuit breakers, chaos, and cache.

    Returns:
        ResilienceStatusResponse: Comprehensive diagnostics on circuit breakers, cache, and LLM quotas.
    """
    await semantic_cache.check_connection()
    statuses = circuit_breaker_registry.get_all_statuses()
    snapshots = {
        name: CircuitSnapshot(
            name=info.name,
            state=info.state,
            fail_counter=info.fail_counter,
            fail_max=info.fail_max,
            is_open=info.is_open,
            last_failure_time=info.last_failure_time,
            last_state_change=info.last_state_change,
            last_failure_reason=info.last_failure_reason,
        )
        for name, info in statuses.items()
    }
    return ResilienceStatusResponse(
        circuits=snapshots,
        chaos_active=chaos_manager.get_config().active,
        cache_driver=semantic_cache.driver,
        cache_entries_count=semantic_cache.count,
        active_model=get_active_model_name(),
        llm_quota=groq_quota_tracker.get_snapshot(),
    )


@app.get("/llm/quota", response_model=LLMQuotaSnapshot)
async def get_llm_quota():
    """Retrieve real-time rate limit headers, token budgets, and failover metrics for LLM providers.

    Returns:
        LLMQuotaSnapshot: Token budgets, rate limits, and fallback statistics.
    """
    return groq_quota_tracker.get_snapshot()


@app.post("/resilience/reset", dependencies=[Depends(verify_admin_key)])
async def reset_resilience():
    """Reset all circuit breakers back to closed, reset chaos configuration, and reset LLM quota counters.

    Returns:
        dict[str, str]: Operation confirmation status dictionary.
    """
    circuit_breaker_registry.reset_all()
    chaos_manager.reset()
    groq_quota_tracker.reset_counters()
    return {
        "status": "ok",
        "message": "Circuit breakers, chaos state, and LLM counters successfully reset",
    }


@app.post("/cache/clear", dependencies=[Depends(verify_admin_key)])
async def clear_cache():
    """Flush all semantic cache entries.

    Returns:
        dict[str, str]: Operation confirmation status dictionary.
    """
    await semantic_cache.clear()
    return {"status": "ok", "message": "Semantic cache successfully cleared"}


@app.get("/metrics", response_class=PlainTextResponse)
async def get_metrics():
    """Prometheus text exposition format endpoint for scraping operational metrics."""
    statuses = circuit_breaker_registry.get_all_statuses()
    lines = [
        "# HELP resilient_triage_requests_total Total incident triage requests handled by gateway",
        "# TYPE resilient_triage_requests_total counter",
        f'resilient_triage_requests_total{{cache="hit"}} {_metrics["requests_hit"]}',
        f'resilient_triage_requests_total{{cache="miss"}} {_metrics["requests_miss"]}',
        f'resilient_triage_requests_total{{cache="saturated"}} {_metrics["requests_saturated"]}',
        "",
        "# HELP resilient_triage_circuit_breaker_state Active circuit breaker state (1 if active, 0 otherwise)",
        "# TYPE resilient_triage_circuit_breaker_state gauge",
    ]

    for name, info in statuses.items():
        st = info.state.lower().replace("-", "_")
        for possible_state in ["closed", "open", "half_open"]:
            val = 1 if st == possible_state else 0
            lines.append(
                f'resilient_triage_circuit_breaker_state{{name="{name}",state="{possible_state}"}} {val}'
            )

    lines.extend(
        [
            "",
            "# HELP resilient_triage_circuit_breaker_fail_counter Current consecutive failure count",
            "# TYPE resilient_triage_circuit_breaker_fail_counter gauge",
        ]
    )
    for name, info in statuses.items():
        lines.append(
            f'resilient_triage_circuit_breaker_fail_counter{{name="{name}"}} {info.fail_counter}'
        )

    lines.extend(
        [
            "",
            "# HELP resilient_triage_cache_entries Total incident reports persist in semantic cache",
            "# TYPE resilient_triage_cache_entries gauge",
            f'resilient_triage_cache_entries{{driver="{semantic_cache.driver}"}} {semantic_cache.count}',
            "",
            "# HELP resilient_triage_llm_rate_limit_hits_total Total HTTP 429 rate limit events encountered",
            "# TYPE resilient_triage_llm_rate_limit_hits_total counter",
            f"resilient_triage_llm_rate_limit_hits_total {groq_quota_tracker.rate_limit_hits}",
            "",
            "# HELP resilient_triage_llm_failovers_total Total fallback model invocations triggered",
            "# TYPE resilient_triage_llm_failovers_total counter",
            f"resilient_triage_llm_failovers_total {groq_quota_tracker.failover_events}",
            "",
        ]
    )

    return PlainTextResponse(content="\n".join(lines) + "\n")
