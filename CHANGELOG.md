# Changelog

All notable changes to the `resilient-triage` project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- **Phase 3**: Redis semantic vector search caching ($\ge 0.90$ cosine similarity) with sub-20ms latency target.
- **Phase 4**: LangGraph cyclic state machine with degraded fallback routing and LLM self-repair retry loop (capped at 2 retries).
- **Phase 5**: FastAPI REST gateway with OpenAPI documentation and full chaos testing suite.

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
