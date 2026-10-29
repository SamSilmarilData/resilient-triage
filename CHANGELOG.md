# Changelog

All notable changes to the `resilient-triage` project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Report reconciliation owns the degradation fields**: `finalize` now runs `reconcile` (`graph/reconciliation.py`) on every report, so `degradation_status` and `circuit_breakers_tripped` follow one rule whichever path produced the report. The LLM no longer controls either field: its values, including breaker names it made up, are discarded.
- **Any non-diagnostic action now requires approval**: report reconciliation sets `requires_approval=True` on every recommended action whose `action_type` is not `diagnostic` (case and whitespace ignored). Previously only the exact value `remediative` was caught, so labels like `"Remediation"` or `"mutating"` slipped through. The check moved out of `validate_report`.
- **Only targeted breakers degrade a report**: an open circuit breaker for a telemetry source the query does not target no longer marks the report degraded or lists that breaker.
- **Circuit breakers sit behind a project-owned interface**: `AsyncCircuitBreaker` (`resilience/circuit_breaker.py`) is now the only module that imports `pybreaker`. A refused call raises the project's `BreakerOpen` instead of `pybreaker.CircuitBreakerError`, and the breaker surface is `call`, `name`, `state`, `info()` and `reset()`. The Statuspage and Cluster Sentinel sources degrade exactly as before. The `pybreaker` constraint is now `>=1.3.0,<1.5` (1.2.0 raised `TypeError` from an open breaker).
- **Each app instance owns one runtime**: `create_app()` builds the gateway, and its lifespan starts one `AppRuntime` (cache, breakers, chaos, LLM gateway, telemetry clients) and closes it on shutdown. Routes, responses and headers are unchanged. The graph concurrency limit (10) and the `/metrics` request counters are now per app, and `/metrics` reads the same resilience snapshot as `/health`, so it pings Redis once per scrape like the other status routes.
- **No module-level singletons remain**: `semantic_cache`, `circuit_breaker_registry`, `chaos_manager`, `groq_quota_tracker`, the shared Statuspage and Groq HTTP clients, the process-wide Statuspage summary cache, and `set_triage_llm` / `get_triage_llm` / `get_active_model_name` are removed, with their package re-exports and close/clear helpers. `StatuspageClient` now requires its HTTP client and keeps its own summary cache and breaker; `ChaosClient` requires its `ChaosManager`; the LLM gateway owns its adapter and HTTP client. Only `settings`, the embedder and `triage_graph` stay process-wide; everything else lives on `AppRuntime`.
- **Every model call goes through one LLM gateway** (ADR-006): the new `resilient_triage.llm` package replaces `graph/llm.py`. `LLMGateway.complete(messages)` returns a `Completion` or a typed provider failure (`RateLimited`, `Unavailable`, `Rejected`, `MalformedResponse`) and never raises except `CancelledError`. `GroqAdapter` makes one HTTP call to one model and reads the rate-limit headers; `ScriptedAdapter` is the test fake and the zero-config simulation. `build_llm_gateway` is the one place a provider is chosen, and `build_runtime(llm_adapter=...)` takes a model adapter. The gateway owns the failover policy, by the primary's result: a rate limit cools the primary down and calls the fallback model; `Unavailable` retries the primary once after a short jittered backoff, then calls the fallback; `MalformedResponse` calls the fallback; `Rejected` is returned at once. A `complete()` makes at most 3 calls, skips a cooling-down primary, and records nothing when cancelled. Cooldowns are per model and sized by the rate limit's scope (`minute`: `retry-after`, else the tokens bucket's reset; `day`: the requests bucket's reset; otherwise the default), and every cooldown is capped. Model calls are no longer wrapped in `with_retry`. The quota snapshot's `remaining_*`, `limit_*`, `reset_*` and `last_status` now describe the primary model only, and `active_model` names the model that served the last completion. See `docs/resilience.md` section 6. New settings, all in `.env.example`:
  - `LLM_REQUEST_TIMEOUT_S` (default 20s, at most 120s): the Groq HTTP timeout, previously hard-coded.
  - `GROQ_FALLBACK_MODEL` (default `llama-3.1-8b-instant`): the fallback model, previously hard-coded. ADR-006 supersedes ADR-005's `openai/gpt-oss-20b`.
  - `LLM_DEFAULT_COOLDOWN_S` (default 30s, 1–3600): the cooldown when a 429 gives no usable wait.
  - `LLM_MAX_COOLDOWN_S` (default 3600s, 1–86400): the cap on every cooldown.
- **Unavailable sources degrade a report**: a targeted telemetry source that fails (its fetch raised or returned an `UNAVAILABLE` signal) while its breaker is still closed now makes the report at least `PARTIALLY_DEGRADED`; previously the report could claim `HEALTHY`.

### Removed
- **OpenAI and Gemini providers**: `OPENAI_API_KEY`, `GOOGLE_API_KEY` and `GEMINI_API_KEY` no longer select a provider, and the provider branches that needed `langchain_openai` / `langchain_google_genai` (never dependencies) are gone. Only `GROQ_API_KEY` selects a real model; without it the app runs the zero-config simulation.
- **The LangChain model classes**: `GroqChatModel`, `GroqQuotaTracker`, `MockTriageChatModel` and `build_triage_llm`, with `graph/llm.py` and its re-exports from `resilient_triage.graph`.

### Fixed
- A 429 is no longer retried against the rate-limited primary model: when every call is rate limited, the gateway sends exactly one call to the primary and one to the fallback. Previously it went back to the primary after the fallback failed, making up to 4 calls (bug-hunt finding 2).
- The fallback model's 429 no longer puts the primary into cooldown, and the quota snapshot no longer shows the fallback's budget as the primary's (bug-hunt finding 3).
- Groq rate-limit headers are now parsed correctly. Reset headers are read as Go-style durations, so `2m59.56s` is 179.56s; it used to be read as 59.56s, and `1h2m3s`, `120ms`, `1h` or a bare `2` fell back to the default. A 429's scope and wait come from `retry-after` first, then from whichever bucket has 0 remaining (`-requests` is the daily bucket, `-tokens` the per-minute one; both at 0 counts as daily); a `retry-after` with no bucket at 0 counts as per-minute. Previously every Groq 429 got the 30s default cooldown, whatever its headers said.
- The `/health` label now always names the model that runs: `Groq LPU (<model>)` with a Groq key, else `Zero-Config SRE Simulation`. Previously an OpenAI or Gemini key could pick a provider whose package is not a dependency, or show a label that disagreed with the model that ran (bug-hunt finding 4).
- Query analysis now matches its source-routing keywords as whole words or phrases, in both the query and `service_filter`, instead of as substrings. Previously `repo` matched inside "reporting", `actions` inside "transactions" and `auth` inside "author", sending queries to the wrong telemetry sources. Because only targeted breakers degrade a report, a wrong route could return `HEALTHY` while Cluster Sentinel's breaker was open. Hyphens and punctuation separate words (`auth` still matches `auth-service`); underscores do not (`chaos_probe` is one word). Plurals no longer match their singular keyword (`webhooks`, `repos`, `databases`, `payments`), and a query with no keyword match targets both sources.
- `BreakerOpen` now means only that the breaker refused a call without reaching the source. The call whose failure trips a breaker open (including a failed half-open trial) now raises the source's own error instead of `BreakerOpen`, so the Statuspage and Cluster Sentinel sources report that call as `UNAVAILABLE` with the real error rather than `CIRCUIT_OPEN` with "failing fast". Report degradation is unchanged: it follows the breaker's state, which is open either way.
- A half-open circuit breaker now lets exactly one trial call reach the recovering source. Callers that arrive while the trial is running get `BreakerOpen` immediately instead of all passing through. A failed trial re-opens the breaker; a cancelled trial leaves it half-open so the next caller becomes the new trial. Only the trial's outcome settles the breaker: a call that started before the breaker opened and finishes during the trial no longer closes or re-opens it. Resetting a breaker while its trial is running frees the trial slot, so the next half-open cycle is not blocked by the stale trial.
- A cancelled call no longer counts against a circuit breaker: when a guarded call is interrupted by `CancelledError` (run deadline, client disconnect) or another non-`Exception` `BaseException`, the breaker records neither a success nor a failure and the exception propagates. Previously a single cancellation could trip a `fail_max=1` breaker open.
- A synthesis fallback report with one tripped breaker is now `SEVERELY_DEGRADED`; previously `finalize` downgraded it to `PARTIALLY_DEGRADED`.
- The emergency fallback report (built by `POST /triage` when the graph returns no report) is now reconciled too, so it is `SEVERELY_DEGRADED` and lists tripped targeted breakers instead of being returned and cached as `HEALTHY`.

## [1.3.0] - 2026-10-12 — Editorial Vector Visualizations & Documentation Upgrade

### Added
- **System Architecture Visual Schematics**:
  - `docs/architecture-diagram.svg` & `docs/architecture-diagram.html`: High-fidelity, skinnable 5-column, 3-tier system topology map illustrating sub-20ms dual-engine vector caching highway, guarded telemetry probing loop, Groq LPU inference with quota failover, and strict contract validation.
- **LangGraph State Machine & Resilience Lifecycle Map**:
  - `docs/state-machine-diagram.svg` & `docs/state-machine-diagram.html`: 3-band Lifecycle Phase Map capturing the primary execution rail (happy path), adaptive recovery detours (Cache 0.70 blindspot compensation and Pydantic self-repair loops), and terminal resolution outcomes (`finalize_report`, `fallback_report`).
- **Interactive Navigation & Responsive Summary Cards**:
  - Standalone HTML views with responsive navigation tabs, editorial summary cards, and dark console styling matching the SRE console theme (`#09090b` canvas, `#18181b` containers, `#fafafa` ink, `#a78bfa` accent).
- **Client Design Profile**:
  - `.diagram-design`: Configured project profile marker pointing to `resilient-triage` design tokens.

### Changed
- **Documentation Overhaul**:
  - `README.md`: Replaced generic Mermaid flowchart with native SVG embed of the system architecture topology and added a dedicated LangGraph lifecycle diagram section.
  - `docs/architecture.md`: Replaced Mermaid blocks in Section 2 and Section 5 with vector diagram embeddings and interactive view links.
  - `docs/resilience.md`: Replaced overview Mermaid flowchart in Section 1 with the complete state machine lifecycle diagram while preserving the 3-state Pybreaker finite state machine as a micro-spec in Section 2.
  - `docs/demo.md`: Added direct visual architecture schematic links in the executive pitch section.

### Fixed
- **SVG XML Parsing & Markdown Image Embedding Compatibility**:
  - Replaced non-predefined HTML character entities (`&bull;`, `&rarr;`, `&middot;`, `&ge;`) with numeric XML character entities (`&#8226;`, `&#8594;`, `&#183;`, `&#8805;`) across `docs/architecture-diagram.svg` and `docs/state-machine-diagram.svg` to satisfy strict XML parsing requirements (`image/svg+xml`).
  - Added explicit intrinsic `width="1040"` and `height="540"` attributes to root `<svg>` elements to guarantee proper aspect ratio scaling and avoid zero-height collapsing across markdown previewers and browsers.

---

## [1.2.0] - 2026-10-10 — Comprehensive Documentation, MADR ADRs, API Reference, & Groq Quota Tracking

### Added
- **Architecture Decision Records (MADRs)** in `docs/decisions/`:
  - `0001-langgraph-cyclic-state-machine.md`: Decision rationale, alternatives considered, and validation guarantees for LangGraph state machine.
  - `0002-dual-engine-semantic-caching.md`: Sub-20ms Redis 8 RediSearch HNSW indexing with instant in-memory NumPy fallback.
  - `0003-pybreaker-and-tenacity-for-resilience.md`: Inverted nesting of Tenacity exponential backoff retries with randomized jitter inside Pybreaker circuit breakers.
  - `0004-strict-pydantic-contracts-and-self-repair.md`: Strict Pydantic v2 schemas (`extra="forbid"`) and self-repair feedback loop.
  - `0005-free-tier-groq-quota-monitoring-and-failover.md`: Real-time tracking of Groq free-tier rate limit headers, token budgets, and 429 failover to `openai/gpt-oss-20b`.
- **Standalone REST API Reference (`docs/api.md`)**:
  - Full OpenAPI specification detailing all 8 API endpoints, HTTP methods, headers, schemas, and curl examples.
- **Resilience Engineering Deep-Dive (`docs/resilience.md`)**:
  - Deep-dive guide explaining finite state machines, full jitter exponential backoff formulas, and zero-downtime cache failovers.
- **Documentation Hub in `README.md`**:
  - Centralized index referencing architecture, API specs, resilience mechanics, schemas, and ADRs.
- **Codebase Docstring Augmentation**:
  - Standardized Google/PEP-257 docstrings with typed `Args:`, `Returns:`, and `Raises:` blocks across all modules in `src/resilient_triage/`.
- **Expanded Test Battery**:
  - Added test coverage for Groq quota tracking, reset endpoints, and model failovers, bringing suite to 58/58 passing tests.

---

## [1.1.0] - 2026-10-03 — Performance & Dual-Metric Observability

### Highlights
- Slashed end-to-end latency across cloud deployments (e.g. Render Singapore):
  - **Cache Hit Turnaround**: Reduced to **`1.16ms`** (Redis HNSW + In-Memory LRU Vector Cache), well below the 20ms target.
  - **Cache Miss Turnaround**: Reduced from ~2.8s to **`1.27s`** (~55% faster) via persistent Groq connection pooling and 10s Statuspage caching.
- Surfaced **Dual-Metric Latency Telemetry** across the SRE Command Center UI, cleanly isolating server execution from physical internet WAN RTT.
- Expanded automated regression test suite to **53/53 passing tests** in 4.09s.

### Added
- **Statuspage 10-Second Short-TTL In-Memory Cache**:
  - `src/resilient_triage/telemetry/statuspage.py`: In-memory cache for parsed `StatuspageSummary` records with 10-second TTL. Eliminates redundant ~700ms external network calls to GitHub Status API on rapid triage queries.
  - Added `clear_statuspage_cache()` and test coverage in `tests/test_telemetry.py`.
- **In-Memory Embedding LRU Cache & Preset Pre-Warming**:
  - `src/resilient_triage/cache/embeddings.py`: Added 512-entry `OrderedDict` LRU cache to `FastEmbedProvider` and `DeterministicEmbeddingProvider`.
  - `src/resilient_triage/api/app.py`: Automatically pre-warms quick incident presets (GitHub Actions, Stripe, PostgreSQL) during FastAPI startup (`lifespan`), ensuring instant 0.001ms vector resolution for demo visitors.
  - Added unit test `test_embedding_provider_lru_cache` in `tests/test_cache.py`.
- **Interactive Demo Script & Guide**:
  - `docs/demo.md`: Complete 5-Act presentation script and SRE walkthrough guide for live demos and interviews.

### Changed
- **Groq LPU Client Optimization**:
  - `src/resilient_triage/graph/llm.py`: Replaced per-request HTTP client creation with a persistent, keep-alive pooled `httpx.AsyncClient` (`limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)`).
  - Added `max_tokens=650` to Groq LPU completion payload to eliminate runaway token generation while producing complete, structured incident reports.
- **SRE Command Center Observability**:
  - `src/resilient_triage/api/templates/index.html`: Updated glowing telemetry banner and live audit stream to display dual-metric precision: `Server: 1.16ms (Redis HNSW) • Net RTT: 78ms (79ms total)` and `POST /triage 200 (Srv: 1ms | Net: 78ms)`.
  - Bumped UI version tag to `v1.1.0`.
- **Version Unification**:
  - Unified package version to `1.1.0` across `pyproject.toml`, `src/resilient_triage/__init__.py`, `src/resilient_triage/api/app.py`, `docs/schemas.md`, and `tests/test_api.py`.

---

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
