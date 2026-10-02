# Changelog

All notable changes to the `resilient-triage` project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- **Phase 5**: FastAPI REST gateway with OpenAPI documentation and full chaos testing suite.

---

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
