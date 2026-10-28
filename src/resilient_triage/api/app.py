"""FastAPI application for Resilient Incident Triage Gateway."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from langchain_core.runnables import RunnableConfig

from resilient_triage import __version__
from resilient_triage.config import settings
from resilient_triage.graph.builder import triage_graph
from resilient_triage.graph.reconciliation import DegradationEvidence, reconcile
from resilient_triage.runtime import AppRuntime, build_runtime
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

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

RuntimeFactory = Callable[[], AppRuntime]

router = APIRouter()


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


def get_runtime(request: Request) -> AppRuntime:
    """Return the runtime owned by the app serving this request."""
    return cast(AppRuntime, request.app.state.runtime)


RuntimeDep = Annotated[AppRuntime, Depends(get_runtime)]


async def process_time_and_correlation_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Add a request ID, elapsed time, and a structured error fallback to every response."""
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


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
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


@router.post("/triage", response_model=TriageResponse)
async def triage_incident(
    payload: TriageRequest,
    request: Request,
    runtime: RuntimeDep,
) -> JSONResponse:
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
        match = await runtime.cache.lookup(
            query=payload.query,
            service_filter=payload.service_filter,
        )
        if match:
            runtime.requests.answered_from_cache += 1
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
        await asyncio.wait_for(runtime.graph_semaphore.acquire(), timeout=5.0)
    except TimeoutError:
        runtime.requests.shed += 1
        logger.warning("No run slot freed up in time; shed request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Triage state machine is currently saturated. "
                "Please retry with exponential backoff."
            ),
        ) from None

    try:
        initial_state: TriageState = {
            "query": payload.query,
            "service_filter": payload.service_filter,
        }
        # Isolate per-execution thread to prevent previous query errors from contaminating state
        execution_thread_id = f"{thread_id}_{uuid.uuid4().hex[:8]}"
        config: RunnableConfig = {"configurable": {"thread_id": execution_thread_id}}
        state_result = await triage_graph.ainvoke(
            initial_state, config=config, context=runtime.deps
        )
    finally:
        runtime.graph_semaphore.release()

    runtime.requests.answered_by_run += 1

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
        fallback_input.update(fallback_state)
        # Second reconciliation call site (after finalize): the graph never reached it.
        final_report = reconcile(
            fallback_state["final_report"],
            DegradationEvidence.from_state(cast(TriageState, fallback_input)),
        )
        # Persist emergency fallback report to semantic cache
        await runtime.cache.store(
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


@router.post("/chaos/inject", response_model=ChaosConfig, dependencies=[Depends(verify_admin_key)])
async def inject_chaos(payload: ChaosTriggerRequest, runtime: RuntimeDep) -> ChaosConfig:
    """Dynamically update runtime chaos injection parameters.

    Args:
        payload: New ChaosTriggerRequest parameters (latency, 503 injection, failure rate).
        runtime: The app's runtime, whose chaos manager is updated.

    Returns:
        ChaosConfig: Updated active configuration snapshot.
    """
    return runtime.chaos.update_config(payload)


@router.get("/chaos/config", response_model=ChaosConfig)
async def get_chaos_config(runtime: RuntimeDep) -> ChaosConfig:
    """Retrieve active runtime chaos configuration.

    Args:
        runtime: The app's runtime, whose chaos manager is read.

    Returns:
        ChaosConfig: Current chaos settings including latency, error toggles, and status.
    """
    return runtime.chaos.get_config()


@router.get("/chaos/probe", response_model=ChaosTelemetryResponse)
async def probe_chaos(runtime: RuntimeDep) -> ChaosTelemetryResponse:
    """Execute live diagnostic probe through Tenacity retries and circuit breaker.

    Args:
        runtime: The app's runtime, which probes Cluster Sentinel.

    Returns:
        ChaosTelemetryResponse: Diagnostic outcome from the internal chaos service.
    """
    try:
        return await runtime.probe_cluster_sentinel()
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


@router.get("/", response_class=HTMLResponse)
async def get_sre_console() -> HTMLResponse:
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


@router.get("/health", response_model=HealthResponse)
async def get_health(runtime: RuntimeDep) -> HealthResponse:
    """Health check endpoint indicating service availability and breaker status.

    Args:
        runtime: The app's runtime, snapshotted once (one Redis ping).

    Returns:
        HealthResponse: Status indicator, Redis connection state, active driver, and LLM quota.
    """
    snapshot = await runtime.snapshot()
    any_open = any(info.is_open for info in snapshot.breakers.values())

    return HealthResponse(
        status="degraded" if any_open else "healthy",
        version=__version__,
        redis_connected=snapshot.cache_connected,
        cache_driver=snapshot.cache_driver,
        circuits={name: info.state for name, info in snapshot.breakers.items()},
        active_model=snapshot.active_model,
        llm_quota=snapshot.llm_quota,
    )


@router.get("/resilience/status", response_model=ResilienceStatusResponse)
async def get_resilience_status(
    runtime: RuntimeDep,
) -> ResilienceStatusResponse:
    """Detailed resilience diagnostic report for circuit breakers, chaos, and cache.

    Args:
        runtime: The app's runtime, snapshotted once (one Redis ping).

    Returns:
        ResilienceStatusResponse: Diagnostics on circuit breakers, cache, and LLM quotas.
    """
    snapshot = await runtime.snapshot()
    circuits = {
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
        for name, info in snapshot.breakers.items()
    }
    return ResilienceStatusResponse(
        circuits=circuits,
        chaos_active=snapshot.chaos_active,
        cache_driver=snapshot.cache_driver,
        cache_entries_count=snapshot.cache_entries,
        active_model=snapshot.active_model,
        llm_quota=snapshot.llm_quota,
    )


@router.get("/llm/quota", response_model=LLMQuotaSnapshot)
async def get_llm_quota(runtime: RuntimeDep) -> LLMQuotaSnapshot:
    """Retrieve real-time rate limit headers, token budgets, and failover metrics for LLM providers.

    Args:
        runtime: The app's runtime, whose quota tracker is read.

    Returns:
        LLMQuotaSnapshot: Token budgets, rate limits, and fallback statistics.
    """
    return runtime.quota.get_snapshot()


@router.post("/resilience/reset", response_model=None, dependencies=[Depends(verify_admin_key)])
async def reset_resilience(runtime: RuntimeDep) -> dict[str, Any]:
    """Reset all circuit breakers to closed, and reset chaos configuration and LLM quota counters.

    Args:
        runtime: The app's runtime, whose breakers, chaos manager and quota tracker are reset.

    Returns:
        dict[str, str]: Operation confirmation status dictionary.
    """
    runtime.breakers.reset_all()
    runtime.chaos.reset()
    runtime.quota.reset_counters()
    return {
        "status": "ok",
        "message": "Circuit breakers, chaos state, and LLM counters successfully reset",
    }


@router.post("/cache/clear", response_model=None, dependencies=[Depends(verify_admin_key)])
async def clear_cache(runtime: RuntimeDep) -> dict[str, str]:
    """Flush all semantic cache entries.

    Args:
        runtime: The app's runtime, whose semantic cache is cleared.

    Returns:
        dict[str, str]: Operation confirmation status dictionary.
    """
    await runtime.cache.clear()
    return {"status": "ok", "message": "Semantic cache successfully cleared"}


@router.get("/metrics", response_class=PlainTextResponse)
async def get_metrics(runtime: RuntimeDep) -> PlainTextResponse:
    """Prometheus text exposition format endpoint for scraping operational metrics."""
    snapshot = await runtime.snapshot()
    statuses = snapshot.breakers
    counts = runtime.requests
    lines = [
        "# HELP resilient_triage_requests_total Total incident triage requests handled by gateway",
        "# TYPE resilient_triage_requests_total counter",
        f'resilient_triage_requests_total{{cache="hit"}} {counts.answered_from_cache}',
        f'resilient_triage_requests_total{{cache="miss"}} {counts.answered_by_run}',
        f'resilient_triage_requests_total{{cache="saturated"}} {counts.shed}',
        "",
        (
            "# HELP resilient_triage_circuit_breaker_state Active circuit breaker state (1 if "
            "active, 0 otherwise)"
        ),
        "# TYPE resilient_triage_circuit_breaker_state gauge",
    ]

    for name, info in statuses.items():
        st = info.state.lower().replace("-", "_")
        for possible_state in ["closed", "open", "half_open"]:
            val = 1 if st == possible_state else 0
            labels = f'name="{name}",state="{possible_state}"'
            lines.append(f"resilient_triage_circuit_breaker_state{{{labels}}} {val}")

    lines.extend(
        [
            "",
            (
                "# HELP resilient_triage_circuit_breaker_fail_counter Current consecutive "
                "failure count"
            ),
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
            (
                "# HELP resilient_triage_cache_entries Total incident reports persist in "
                "semantic cache"
            ),
            "# TYPE resilient_triage_cache_entries gauge",
            (
                f'resilient_triage_cache_entries{{driver="{snapshot.cache_driver}"}} '
                f"{snapshot.cache_entries}"
            ),
            "",
            (
                "# HELP resilient_triage_llm_rate_limit_hits_total Total HTTP 429 rate limit "
                "events encountered"
            ),
            "# TYPE resilient_triage_llm_rate_limit_hits_total counter",
            f"resilient_triage_llm_rate_limit_hits_total {snapshot.llm_quota.rate_limit_hits}",
            "",
            (
                "# HELP resilient_triage_llm_failovers_total Total fallback model invocations "
                "triggered"
            ),
            "# TYPE resilient_triage_llm_failovers_total counter",
            f"resilient_triage_llm_failovers_total {snapshot.llm_quota.failover_events}",
            "",
        ]
    )

    return PlainTextResponse(content="\n".join(lines) + "\n")


def _default_runtime() -> AppRuntime:
    """Build the production runtime from the process settings."""
    return build_runtime(settings)


def create_app(runtime_factory: RuntimeFactory = _default_runtime) -> FastAPI:
    """Build the gateway app; its lifespan starts one runtime from the factory and closes it."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Initializing semantic cache and warming up embedding models...")
        runtime = runtime_factory()
        await runtime.start()
        app.state.runtime = runtime
        logger.info("Resilient Triage Gateway online and ready for traffic.")
        try:
            yield
        finally:
            logger.info("Shutting down Resilient Triage Gateway...")
            await runtime.aclose()

    app = FastAPI(
        title="Resilient Incident Triage Gateway",
        description=(
            "High-availability LangGraph incident triage state machine with Redis semantic "
            "caching and circuit breaker telemetry."
        ),
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
    app.middleware("http")(process_time_and_correlation_middleware)
    app.add_exception_handler(Exception, global_exception_handler)
    app.include_router(router)
    return app


app = create_app()
