# Changelog

All notable changes to the `resilient-triage` project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- **Phase 2**: Tenacity retry wrappers with jitter and Pybreaker circuit breakers for telemetry tools.
- **Phase 2**: Live Atlassian Statuspage integration and configurable `/chaos` endpoint.
- **Phase 3**: Redis semantic vector search caching ($\ge 0.90$ cosine similarity) with sub-20ms latency target.
- **Phase 4**: LangGraph cyclic state machine with degraded fallback routing and LLM self-repair retry loop (capped at 2 retries).
- **Phase 5**: FastAPI REST gateway with OpenAPI documentation and full chaos testing suite.

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
