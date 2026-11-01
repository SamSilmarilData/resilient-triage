# 🛡️ resilient-triage

> **Fault-tolerant incident triage gateway powered by LangGraph, Groq LPUs, and Redis 8 RediSearch that doesn't collapse when external systems are on fire.**

[![Release v2.0.0](https://img.shields.io/badge/Release-v2.0.0-blue.svg?style=flat)](https://github.com/SamSilmarilData/resilient-triage/releases/tag/v2.0.0)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.135.1+-009688.svg?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-FF6F00.svg?style=flat&logo=chainlink&logoColor=white)](https://langchain-ai.github.io/langgraph/)
[![Redis 8](https://img.shields.io/badge/Redis_8-RediSearch_HNSW-DC382D.svg?style=flat&logo=redis&logoColor=white)](https://redis.io/)
[![Groq LPU](https://img.shields.io/badge/Groq_LPU-Qwen_3.8_27B-F55036.svg?style=flat)](https://groq.com)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-E92063.svg?style=flat&logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![Tests Passing](https://img.shields.io/badge/Tests-402_Passed-10B981.svg?style=flat)](https://github.com/SamSilmarilData/resilient-triage)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat)](LICENSE)

---

## ⚡ Live Performance Highlights (v1.3.0)

| Metric | Target | Measured Live Performance | Verification |
| :--- | :---: | :---: | :--- |
| **Semantic Cache Lookup (Hit)** | `< 20ms` | **`1.16 ms`** | Redis HNSW + In-Memory LRU Vector Cache |
| **Live LLM Triage (Miss)** | `< 2.0s` | **`1.27 s`** | Groq LPU keep-alive pool + 10s Statuspage cache + token cap |
| **Time-To-First-Token (TTFT)** | `< 500ms` | **`166 ms`** | Groq LPUs via async JSON streaming |
| **Disconnect Resilience** | `Zero 500s` | **`0 Errors`** | Mid-flight Redis drop tested with 100% in-memory failover |
| **Test Suite Coverage** | `100%` | **`85 / 85 Passing`** | Full pytest regression suite completes in **`1.8s`** |

---

## 🖥️ Explorable Page (Served at `/`)

`/` serves a build-free page (ADR-010) that explains a triage run by playing it: guided scenarios break one dependency at a time while a graph and a timeline draw the real execution trace, then free play runs your own question. The page shell is in place (static prose, layout, light and dark themes); the scenarios, trace view and free play are being built on top of it (tracking issue #104). Everything it loads, Preact, htm, Signals and the Inter and JetBrains Mono fonts included, is vendored and served from the app's own origin.

---

## 🏛️ System Architecture

<p align="center">
  <a href="docs/architecture-diagram.html">
    <img src="docs/architecture-diagram.svg" alt="Resilient Incident Triage Architecture" width="100%">
  </a>
</p>
<p align="center">
  <em>Figure 1: Full-system topology showing ingress routing, dual-engine semantic caching highway (Redis 8 & NumPy fallback), circuit-breaker guarded telemetry probes, and strict Pydantic validation. <a href="docs/architecture-diagram.html">Open interactive view &rarr;</a></em>
</p>

### 🔄 LangGraph State Machine & Resilience Lifecycle

<p align="center">
  <a href="docs/state-machine-diagram.html">
    <img src="docs/state-machine-diagram.svg" alt="LangGraph State Machine Lifecycle" width="100%">
  </a>
</p>
<p align="center">
  <em>Figure 2: LangGraph cyclic state machine lifecycle showing the primary execution rail, adaptive recovery detours (blindspot compensation & self-repair), and terminal resolution outcomes. <a href="docs/state-machine-diagram.html">Open interactive view &rarr;</a></em>
</p>

---

## 🛡️ Core Resilience Mechanics

1. **Redis 8 RediSearch Semantic Caching**:
   - Computes dense 384-dimensional query embeddings in ~3.5ms via FastEmbed ONNX runtime (`BAAI/bge-small-en-v1.5`).
   - RediSearch HNSW vector indexing performs KNN similarity searches directly inside Redis in **`7.71ms`**.
   - If Redis crashes or disconnects during live traffic, `SemanticCacheManager` intercepts socket errors and seamlessly fails over to an in-memory NumPy vector matrix with **0 dropped requests and 0 HTTP 500 errors**.
   - There is no background probe: once Redis recovers, the next cache lookup or store reconnects, as does `check_connection()` from `/health`, `/resilience/status` and `/metrics`, at most once per 5-second cooldown.
2. **Pybreaker Circuit Breakers**:
   - Guards external telemetry probes against systemic failures (`fail_max=3`, `reset_timeout=30s`).
   - If an external status page crashes or hangs, the breaker trips `OPEN`, causing the LangGraph state machine to route into `DegradedFallbackNode` instead of stalling requests.
3. **Tenacity Exponential Backoff & Jitter**:
   - Telemetry clients absorb transient network drops and HTTP 429 rate limits via exponential backoff with full randomized jitter. Model calls go through the LLM gateway, which retries an outage once and fails over to a second Groq model on a 429 ([resilience guide](docs/resilience.md#6-llm-gateway-failover-and-rate-limit-cooldowns)).
4. **Pydantic Validation & Automated Self-Repair**:
   - All LLM outputs are strictly validated against `IncidentTriageReport`.
   - Any `ValidationError` extracts exact error paths and feeds the traceback back to the LLM context to iteratively repair malformed fields (capped at 2 retries).
5. **Dual-Mode Self-Booting Docker Image**:
   - `Dockerfile` packages `redis-server` directly inside the container.
   - When deployed to single-container platforms (Hugging Face Spaces, Render Free, Railway), `docker-entrypoint.sh` automatically boots embedded Redis with `maxmemory 128mb` and `allkeys-lru` eviction.
   - If an external `REDIS_URL` is provided (e.g. Upstash or Docker Compose), internal Redis is bypassed automatically.
6. **Telemetry & Embedding Vector Optimization (v1.3.0)**:
   - **10-Second Statuspage In-Memory Cache**: Eliminates redundant transcontinental network fetches to status APIs, saving ~700ms on successive triage queries.
   - **Persistent Groq HTTP Connection Pooling**: Keeps connections warm and caps generation at `LLM_MAX_TOKENS` (default 2500), eliminating TCP/TLS handshake latency.
   - **In-Memory LRU Vector Cache & Pre-Warming**: Caches up to 512 query vectors and pre-warms demo presets at boot, driving cache-hit lookups down to **1.16ms**.

---

## 🚀 Quick Start

### Option 1: Native macOS (Zero Docker Desktop Needed)

```bash
# 1. Clone the repository
git clone https://github.com/SamSilmarilData/resilient-triage.git
cd resilient-triage

# 2. Configure environment (Groq API Key)
cp .env.example .env
# Edit .env and paste your GROQ_API_KEY=gsk_...

# 3. Start Redis & Application with 1 script
./scripts/run_local.sh
```

- **Explorable page**: [http://127.0.0.1:8000](http://127.0.0.1:8000)
- **Interactive OpenAPI Documentation**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Live Health Endpoint**: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

To check the installed package as CI's Docker job does, without Docker, run `./scripts/check_package.sh` (or `./scripts/check.sh --package`): it builds the wheel, installs it into a throwaway venv, verifies the vendored files and runs the smoke test against it, with no Redis or Groq key. The image's OS layer and non-root user are checked in CI only. See [docs/deployment.md](docs/deployment.md#check-the-package-without-docker).

---

### Option 2: Docker Compose

For multi-container orchestrations (VPS, EC2, local Docker):

```bash
docker compose up -d --build
```
- API Container: `http://localhost:8000`
- Dedicated Redis Stack Server: `localhost:6379`

---

## 🧪 Testing & Verification

### Automated Pytest Suite (85 Tests)

```bash
.venv/bin/pytest tests/ -v
```

```
============================== 85 passed in 1.80s ==============================
- tests/test_api.py: 24 passed (FastAPI endpoints, middleware, load shedding, admin auth, metrics)
- tests/test_cache.py: 17 passed (HNSW indexing, LRU caching, disconnect failover, scope isolation, offline fallback)
- tests/test_graph.py: 16 passed (LangGraph state machine, self-repair loops)
- tests/test_schemas.py: 10 passed (Pydantic v2 strict models, confidence bounds, enum validation)
- tests/test_telemetry.py: 9 passed (Statuspage parsing, 10s short-TTL cache, dynamic probe routing)
- tests/test_retry.py: 5 passed (Tenacity backoff, jitter, HTTP 429 retries)
- tests/test_circuit_breaker.py: 4 passed (Pybreaker state transitions, snapshots, half-open probing)
```

### Live Smoke Test Battery (9/9 Steps)

Executes an automated end-to-end verification against the running server or an ephemeral test instance:

```bash
.venv/bin/python scripts/smoke_test.py
```

---

## 📚 Documentation Hub

Explore the architecture, resilience engineering, API specifications, and architectural decision records:

- 📖 **[REST API Reference](docs/api.md)**: Complete OpenAPI specification for all 8 endpoints with request/response schemas and `curl` examples.
- 🛡️ **[Resilience Engineering Deep-Dive](docs/resilience.md)**: Finite state machines, full jitter exponential backoff formulas, and zero-downtime cache failovers.
- 🏛️ **[System Architecture](docs/architecture.md)**: State machine topology, cyclic bounds, dynamic tool dispatch, and unified state management.
- 📋 **[Data Contracts & Schemas](docs/schemas.md)**: Strict Pydantic v2 data models (`extra="forbid"`), enums, and state models.
- 🎬 **[Interactive SRE Demo Guide](docs/demo.md)**: 5-Act presentation script and walkthrough instructions for live demonstrations.
- ☁️ **[Free Cloud Deployment](docs/deployment.md)**: 100% free hosting recipes on Hugging Face Spaces and Render.com.
- ⚖️ **[Architecture Decision Records (MADR)](docs/decisions/)**:
  - [ADR 0001: LangGraph Cyclic State Machine](docs/decisions/0001-langgraph-cyclic-state-machine.md)
  - [ADR 0002: Dual-Engine Semantic Caching](docs/decisions/0002-dual-engine-semantic-caching.md)
  - [ADR 0003: Pybreaker & Tenacity for Resilience](docs/decisions/0003-pybreaker-and-tenacity-for-resilience.md)
  - [ADR 0004: Strict Pydantic Contracts & Self-Repair Loop](docs/decisions/0004-strict-pydantic-contracts-and-self-repair.md)
  - [ADR 0005: Free Tier Groq Quota Monitoring & Model Failover](docs/decisions/0005-free-tier-groq-quota-monitoring-and-failover.md) (fallback model superseded by ADR 0006)
  - [ADR 0006: Project-Owned LLM Gateway](docs/decisions/0006-project-owned-llm-gateway.md)
  - [ADR 0007: No Checkpointing on the Request Path](docs/decisions/0007-no-checkpointing-on-the-request-path.md)
  - [ADR 0008: Execution Trace from a Per-Request Recorder, Streamed over SSE](docs/decisions/0008-execution-trace.md)
  - [ADR 0009: Scenario Runs Are Fully Isolated from the Shared Runtime](docs/decisions/0009-scenario-runs-are-isolated.md)
  - [ADR 0010: Build-Free, Vendored Frontend](docs/decisions/0010-build-free-vendored-frontend.md)

---

## 🌐 API Reference

See the full [REST API Reference](docs/api.md) for detailed JSON schemas, headers, and request flows.

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/triage` | Triage an incident through semantic cache (`<20ms`) or LangGraph state machine. |
| `GET` | `/health` | Health check reporting service status, active model, and Redis connection. |
| `GET` | `/resilience/status` | Deep diagnostics: circuit breaker states, fail counters, active cache driver. |
| `GET` | `/llm/quota` | Real-time rate limit headers, token budgets, and failover metrics for Groq free tier. |
| `GET` | `/metrics` | Prometheus text exposition format for scraping operational metrics and health gauges. |
| `POST` | `/chaos/inject` | Dynamically update runtime chaos parameters (`inject_503`, `failure_rate`, `latency_ms`). |
| `GET` | `/chaos/config` | Inspect active runtime chaos simulation configuration. |
| `GET` | `/chaos/probe` | Trigger live diagnostic probe through Tenacity retries and circuit breaker. |
| `POST` | `/resilience/reset` | Administratively reset all tripped circuit breakers, chaos state, and LLM counters. |
| `POST` | `/cache/clear` | Flush all entries from Redis and in-memory semantic cache. |
| `GET` | `/` | Serve the explorable page (static shell; assets under `/static`). |


---

## 📦 Project Structure

```
resilient-triage/
├── Dockerfile                      # Self-booting Python 3.12-slim container with embedded Redis
├── docker-compose.yml              # Multi-container orchestration spec
├── pyproject.toml                  # Build configuration and dependencies
├── scripts/
│   ├── docker-entrypoint.sh        # Dual-mode container bootstrapper (embedded vs external Redis)
│   ├── run_local.sh                # 1-click native macOS local runner
│   └── smoke_test.py               # 9-step automated end-to-end smoke test suite
├── docs/
│   ├── api.md                      # Complete OpenAPI REST API reference & examples
│   ├── resilience.md               # Fault tolerance, circuit breakers & jitter backoff deep dive
│   ├── architecture.md             # Deep-dive system design & resilience state machine
│   ├── schemas.md                  # Strict Pydantic contracts and schemas
│   ├── demo.md                     # Interactive 5-Act presentation script & demo guide
│   ├── deployment.md               # 100% Free cloud hosting guide (Hugging Face Spaces / Render)
│   └── decisions/                  # MADR Architecture Decision Records (0001 - 0007)
├── src/resilient_triage/
│   ├── config.py                   # Centralized pydantic-settings configuration
│   ├── schemas/                    # Pydantic v2 data contracts (incident, telemetry, state, api)
│   ├── resilience/                 # Tenacity backoff/jitter and AsyncCircuitBreaker wrappers
│   ├── telemetry/                  # Live Statuspage and configurable Chaos clients
│   ├── cache/                      # Redis 8 RediSearch & NumPy vector caching engine
│   ├── graph/                      # LangGraph cyclic state machine and self-repair nodes
│   └── api/                        # FastAPI gateway, middleware, and the page (static/)
└── tests/                          # 58 unit, integration, and chaos resilience tests
```

---

## 🚢 Deployment Guide

Detailed zero-cost hosting instructions for **Hugging Face Spaces** (Free 16GB Docker Space) and **Render.com** are available in [docs/deployment.md](docs/deployment.md).

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.