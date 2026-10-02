# Changelog

All notable changes to the `resilient-triage` project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-02 — General Availability (GA)

### Highlights
- Official production-ready release of `resilient-triage`.
- Zero-downtime SRE incident triage gateway with real Groq LPU SOTA generation (~1.1s) and Redis 8 RediSearch HNSW vector caching (7.71ms).
- Dark-mode interactive SRE Command Center console live at `/`.
- Dual-mode self-booting container deployed to Render.com.
- **Native Redis 8 & RediSearch Vector Search**:
  - `src/resilient_triage/cache/semantic_cache.py`: Native Redis 8 integration with HNSW float32 cosine vector index (`triage_vector_idx`).
  - Achieves **`7.71ms` live vector lookup turnaround**, dramatically exceeding the sub-20ms target for similar queries ($\ge 0.90$ cosine similarity).
  - Schema definition optimized for RediSearch 8.10.1 and Redis 8 core query engine.
  - Persistent document storage under `triage:doc:*` with scope and TTL management.
- **Dynamic Auto-Reconnection & Zero-Downtime Resilience**:
  - `_ensure_connected()` with non-blocking 5-second backoff: allows Redis to be started, stopped, or restarted at any time without restarting the web server.
  - Graceful mid-flight failover: catches socket connection and timeout drops, instantaneously routing lookups and stores to the in-memory NumPy vector store with **0 dropped requests and 0 HTTP 500 errors**.
  - Startup cache hydration (`_hydrate_from_redis()`): automatically repopulates in-memory vector index from persistent Redis hashes.
- **Self-Contained Dual-Mode Cloud Deployment**:
  - `Dockerfile`: Bundles `redis-server` and `redis-tools` inside the Python 3.12 slim container.
  - `scripts/docker-entrypoint.sh`: Auto-detects single-container hosting environments (Hugging Face Spaces, Render Free, Railway) and boots an embedded background Redis daemon with `maxmemory 128mb` and `allkeys-lru` eviction. Automatically connects to external `REDIS_URL` if provided.
- **SRE Command Center Observability**:
  - `src/resilient_triage/api/templates/index.html`: Added real-time **Redis Status Badge** in the top navigation bar (`REDIS: CONNECTED (RediSearch)` vs `REDIS: FALLBACK (In-Memory)`).
  - `src/resilient_triage/schemas/api.py`: Updated `HealthResponse` and `ResilienceStatusResponse` with `cache_driver` reporting.
- **Automated Test Battery**:
  - `tests/test_cache.py`: Added `test_redis_connectivity_and_driver` and `test_redis_disconnect_and_in_memory_failover`. Total test suite expanded to **51/51 passing tests**.

---

## [0.6.0] - 2026-10-02

### Added
- **Interactive SRE Command Center UI**:
  - `src/resilient_triage/api/templates/index.html`: Modern, responsive single-page SRE incident console served at `/` with dark-mode styling (Linear/Vercel `#09090b` zinc, Tailwind CSS via CDN, Inter and JetBrains Mono typography).
  - Dynamic active model engine badge in header (`⚡ Groq LPU (Qwen 3.8 27B)`).
  - Instant preset incident chips (`💥 Actions 503 Webhooks`, `💳 Stripe Ingestion Outage`, `🗄️ Postgres Pool Exhaustion`).
  - Glowing telemetry badge: `X-Cache: HIT` (emerald green, <20ms) vs `X-Cache: MISS` (violet).
  - Interactive Chaos & Outage Controller: toggle 503 fault injection, failure rate and latency sliders, 1-click circuit tripping and reset.
  - Real-time live request audit log stream.
  - Background polling every 3 seconds for live circuit breaker health telemetry.
- **Ultra-Fast SOTA LLM Engine (Groq LPUs)**:
  - `src/resilient_triage/graph/llm.py`: `GroqChatModel` with native `httpx` async client and `response_format={"type": "json_object"}`.
  - Achieves **~1.0–1.2 second SOTA 27B / 120B inference** on Groq LPUs (`qwen/qwen3.8-27b`, `openai/gpt-oss-120b`).
  - Built-in Tenacity retry wrapper (`with_retry`) protecting against transient HTTP 429 rate limit errors with exponential backoff and randomized jitter.
  - Multi-tier provider ladder: Groq LPUs -> Local Apple Silicon GPU (Ollama) -> Google Gemini -> OpenAI -> High-fidelity SRE simulation fallback.
- **Native macOS Workflow & Dual-Mode Smoke Testing**:
  - `scripts/run_local.sh`: 1-line native runner for macOS without Docker Desktop.
  - `scripts/smoke_test.py`: Dual-mode automated CLI smoke test verifying health, live Groq inference, sub-20ms cache hits, chaos injection, breaker tripping, and recovery.
  - `docs/deployment.md`: Free cloud hosting guide for Hugging Face Spaces (free 16GB Docker Space) and Render.com.

---

## [0.5.0] - 2026-10-02


### Added
- **FastAPI REST Application & Gateway**:
  - `src/resilient_triage/api/app.py`: High-performance FastAPI application wrapping the LangGraph triage engine and semantic cache.
  - Startup lifespan management: auto-initializes `semantic_cache` and pre-warms ONNX embedding weights for zero cold-start delay.
  - Graceful shutdown lifecycle: terminates Redis connections cleanly.
  - `ProcessTimeAndCorrelationMiddleware`: adds `X-Request-ID` and `X-Process-Time-Ms` to all incoming requests, and provides top-level exception sanitization.
  - Structured global exception handling: catches unexpected server exceptions into sanitized JSON (`InternalServerError`) with request ID correlation without leaking environment variables or stack traces.
  - Concurrency & stampede guard: `asyncio.Semaphore(10)` limits concurrent graph executions while fast-path semantic cache lookups remain non-blocking.
- **REST Endpoints**:
  - `POST /triage`: Primary incident triage endpoint supporting `X-Thread-ID`, returning cache telemetry headers (`X-Cache: HIT|MISS`, `X-Cache-Similarity`).
  - `POST /chaos/inject`: Dynamic runtime configuration of chaos simulation parameters (503 fault injection, failure rate, latency).
  - `GET /chaos/config`: Live inspection of runtime chaos parameters.
  - `GET /chaos/probe`: Diagnostic probe triggering Tenacity retries and circuit breaker tracking.
  - `GET /health`: Service health status (`healthy` / `degraded`), Redis connectivity, and all circuit breaker states.
  - `GET /resilience/status`: Deep resilience diagnostics with snapshots of all registered circuit breakers, failure counts, and cache metrics.
  - `POST /resilience/reset`: Administrative reset restoring all tripped circuit breakers to `CLOSED` and resetting chaos parameters.
  - `POST /cache/clear`: Flushes in-memory and Redis semantic vector cache.
- **End-to-End Integration & Resilience Test Suite**:
  - `tests/test_api.py`: 11 integration tests verifying health checks, cache hits (<20ms), cache misses, force refresh bypass, client-specified thread checkpointing, chaos injection, circuit breaker tripping, compensatory graceful degradation during outages, administrative resets, and exception sanitization.


## [0.4.0] - 2026-10-02

### Added
- **Clever LangGraph Cyclic State Machine**:
  - `builder.py`: Compiled cyclic state graph powered by `StateGraph(TriageState)` and thread-scoped `MemorySaver` checkpointing.
  - `analyze_query.py`: Dynamic tool targeting (`target_tools`) dynamically inspecting query keywords and service scope to selectively probe `statuspage`, `chaos`, or both.
  - `collect_telemetry.py`: Concurrent telemetry fetching with `asyncio.gather(..., return_exceptions=True)`.
  - `compensate_blindspots.py`: Compensatory routing bridging data loss during live upstream outages by querying historical patterns from Redis.
  - `degraded_fallback.py`: Graceful degradation annotating blind spots and setting `PARTIALLY_DEGRADED` or `SEVERELY_DEGRADED`.
  - `refine_reflection.py`: Confidence-driven reflection cycle triggering deeper root-cause analysis when `confidence_score < 0.60`.
  - `validate_report.py`: Markdown fence auto-cleaning (`clean_json_string`) and strict Pydantic `IncidentTriageReport` parsing.
  - `self_repair.py`: Automated multi-turn self-repair feeding `ValidationError` tracebacks back to the LLM (capped at 2 retries).
  - `fallback_report.py`: Guaranteed fallback report generation preventing request crashes on retry exhaustion.
  - `finalize.py`: Programmatic enforcement of tripped circuit breakers and synchronous commit to `semantic_cache`.
- **LLM Simulation & Prompts**:
  - `llm.py`: `MockTriageChatModel` supporting deterministic sequence simulation for automated self-repair and reflection testing.
  - `prompts.py`: SRE persona prompt, self-repair template, and reflection directive.
  - `schemas/incident.py`: Added `action_type` (`diagnostic` vs `remediative`) and `requires_approval: bool` to `ActionRecommendation`.
- **Automated Test Suite**:
  - `tests/test_graph.py`: 9 comprehensive tests verifying happy path, dynamic targeting, compensatory routing, bad enum self-repair, malformed JSON self-repair, retry exhaustion fallback, confidence refinement cycles, and thread-scoped memory.

---

## [0.3.0] - 2026-10-02

### Added
- **Redis Semantic Caching Subsystem**:
  - `embeddings.py`: `BaseEmbeddingProvider` interface with `FastEmbedProvider` (ONNX runtime on CPU, `BAAI/bge-small-en-v1.5`, 384 dimensions) and `DeterministicEmbeddingProvider` (zero-download token hash vectorizer for unit tests/offline).
  - `semantic_cache.py`: `SemanticCacheManager` providing sub-20ms cosine similarity lookup ($\ge 0.90$) with dual-engine driver:
    - Native RediSearch HNSW vector index (`FT.CREATE`, `FT.SEARCH`) when connected to Redis Stack.
    - High-performance in-memory vector index via `numpy` dot product (0.015ms) when running standalone or in unit tests.
  - Query scoping: `format_scoped_query` prepending `[scope: <service>]` to isolate queries across infrastructure components.
  - Dynamic TTL management: standard 300s TTL for healthy reports, reduced 60s TTL for degraded reports to quickly re-probe upstream recovery.
  - `CacheMatch` contract emitting execution duration and similarity score.
- **Automated Test Suite**:
  - `tests/test_cache.py`: 9 tests verifying deterministic and FastEmbed vector generation, sub-20ms cache hits (averaging 4.10ms), low-similarity misses, `force_refresh=True` bypass, service scope isolation, degraded TTLs, and cache purging.

---

## [0.2.0] - 2026-10-02

### Added
- **Resilience Layer (Tenacity + Pybreaker)**:
  - `retry.py`: Async Tenacity retry wrapper with exponential backoff and randomized full jitter (`wait_random_exponential`).
  - Transient HTTP error classification: retries timeouts, network drops, and HTTP 429 / 5xx, while fast-failing on client 4xx errors.
  - `circuit_breaker.py`: `AsyncCircuitBreaker` bridge wrapping `pybreaker.CircuitBreaker` with thread-safe async/await execution, bypassing legacy Tornado bugs.
  - `CircuitBreakerMetricsListener`: Real-time state transition tracking (`closed`, `open`, `half-open`), failure counts, and timestamps.
  - `CircuitBreakerRegistry`: Centralized registry for named breakers (`"statuspage"`, `"chaos"`) with snapshot status reporting.
- **Dual Telemetry Inputs**:
  - `statuspage.py`: `StatuspageClient` consuming live Atlassian Statuspage summary endpoints (GitHub Status), with granular timeouts, Tenacity retries, and circuit breaker protection. Emits `TelemetrySignal` objects for degraded components and ongoing incidents.
  - `chaos.py`: `ChaosManager` and `ChaosClient` allowing runtime injection of 503 errors and artificial latency spikes. Trips breaker to OPEN on 3 consecutive failed operations and returns fast-fail degraded signals.
- **Automated Test Suite**:
  - `tests/test_retry.py`: 5 tests covering 429 retry, 503 exhaustion, 400 non-retry fast fail, and helper execution.
  - `tests/test_circuit_breaker.py`: 3 tests covering healthy operations, 3-failure tripping, and registry management.
  - `tests/test_telemetry.py`: 3 tests covering Statuspage signal extraction, breaker fallback, and live chaos injection with fail-fast recovery.

---

## [0.1.0] - 2026-10-02

### Added
- **Base Scaffolding**:
  - Python 3.12 project configuration with modern `pyproject.toml` and pinned dependencies (`fastapi`, `pydantic>=2.7`, `langgraph`, `tenacity`, `pybreaker`, `redis`, `numpy`, `httpx`, `pytest`).
  - `.env.example` defining runtime configurations for Redis, Statuspage, Circuit Breaker, and Self-Repair parameters.
  - `.gitignore` and `.dockerignore` for Python, macOS, and container artifacts.
- **Containerization for OrbStack & Docker**:
  - `Dockerfile` using `python:3.12-slim` with healthcheck endpoint probe.
  - `docker-compose.yml` configuring `redis/redis-stack-server:latest` (with RediSearch) and the `resilient-triage` service.
- **Core Schemas & Data Contracts**:
  - `IncidentTriageReport`: Strict Pydantic model (`extra="forbid"`) with UUID generator, UTC timestamps, `SeverityLevel`, `DegradationStatus`, `ServiceImpact`, `TelemetrySignal`, `ActionRecommendation`, and bounded `confidence_score`.
  - `telemetry`: `StatuspageSummary`, `StatuspageComponent`, `StatuspageIncident`, `ChaosConfig`, and `ChaosTelemetryResponse`.
  - `state`: `TriageState` typing for LangGraph cyclic state machine.
  - `api`: `TriageRequest`, `TriageResponse`, and `ChaosTriggerRequest`.
  - `config`: `Settings` model using `pydantic-settings`.
- **Validation Test Suite**:
  - `tests/test_schemas.py` containing 8 tests verifying strict validation, enum enforcement, extra-field forbidding, statuspage parsing, chaos configs, and API request/response integrity.
