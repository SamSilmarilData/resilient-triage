"""FastAPI application for Resilient Incident Triage Gateway."""

import asyncio
from contextlib import asynccontextmanager
import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from resilient_triage.cache.semantic_cache import semantic_cache
from resilient_triage.graph.builder import triage_graph
from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
from resilient_triage.schemas.api import (
    ChaosTriggerRequest,
    CircuitSnapshot,
    HealthResponse,
    ResilienceStatusResponse,
    TriageRequest,
    TriageResponse,
)
from resilient_triage.schemas.telemetry import ChaosConfig, ChaosTelemetryResponse
from resilient_triage.telemetry.chaos import ChaosClient, chaos_manager

logger = logging.getLogger(__name__)

# Concurrency boundary: max 10 concurrent graph executions to prevent stampedes
GRAPH_SEMAPHORE = asyncio.Semaphore(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup, cache pre-warming, and graceful shutdown."""
    logger.info("Initializing semantic cache and warming up embedding models...")
    await semantic_cache.initialize()
    try:
        # Pre-warm embedding model weights so first request has zero cold-start penalty
        semantic_cache.embedding_provider.embed_query("warmup telemetry query")
    except Exception as exc:
        logger.warning("Embedding pre-warmup warning: %s", exc)

    # Ensure default circuit breakers are registered
    circuit_breaker_registry.get_or_create("statuspage")
    circuit_breaker_registry.get_or_create("chaos")

    logger.info("Resilient Triage Gateway online and ready for traffic.")
    yield

    logger.info("Shutting down Resilient Triage Gateway...")
    await semantic_cache.close()


app = FastAPI(
    title="Resilient Incident Triage Gateway",
    description="High-availability LangGraph incident triage state machine with Redis semantic caching and circuit breaker telemetry.",
    version="0.5.0",
    lifespan=lifespan,
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

    # 2. Cache MISS: Acquire concurrency semaphore and run LangGraph
    async with GRAPH_SEMAPHORE:
        initial_state = {
            "incident_query": payload.query,
            "service_filter": payload.service_filter,
        }
        config = {"configurable": {"thread_id": thread_id}}
        state_result = await triage_graph.ainvoke(initial_state, config=config)

    final_report = state_result.get("final_report")
    if not final_report:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Triage state machine failed to produce a valid incident report",
        )

    # 3. Store result in semantic cache (<2ms)
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


@app.post("/chaos/inject", response_model=ChaosConfig)
async def inject_chaos(payload: ChaosTriggerRequest):
    """Dynamically update runtime chaos injection parameters."""
    return chaos_manager.update_config(payload)


@app.get("/chaos/config", response_model=ChaosConfig)
async def get_chaos_config():
    """Retrieve active runtime chaos configuration."""
    return chaos_manager.get_config()


@app.get("/chaos/probe", response_model=ChaosTelemetryResponse)
async def probe_chaos():
    """Execute live diagnostic probe through Tenacity retries and circuit breaker."""
    client = ChaosClient()
    return await client.probe()


@app.get("/health", response_model=HealthResponse)
async def get_health():
    """Health check endpoint indicating service availability and breaker status."""
    statuses = circuit_breaker_registry.get_all_statuses()
    circuit_states = {name: info.state for name, info in statuses.items()}
    any_open = any(info.is_open for info in statuses.values())

    return HealthResponse(
        status="degraded" if any_open else "healthy",
        version="0.5.0",
        redis_connected=semantic_cache.is_redis_connected,
        circuits=circuit_states,
    )


@app.get("/resilience/status", response_model=ResilienceStatusResponse)
async def get_resilience_status():
    """Detailed resilience diagnostic report for circuit breakers, chaos, and cache."""
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
    )


@app.post("/resilience/reset")
async def reset_resilience():
    """Reset all circuit breakers back to closed and reset chaos configuration."""
    circuit_breaker_registry.reset_all()
    chaos_manager.reset()
    return {"status": "ok", "message": "Circuit breakers and chaos state successfully reset"}


@app.post("/cache/clear")
async def clear_cache():
    """Flush all semantic cache entries."""
    await semantic_cache.clear()
    return {"status": "ok", "message": "Semantic cache successfully cleared"}
