# Architecture & System Design

This document details the architectural principles, state machine topology, and resilience patterns implemented in `resilient-triage`.

---

## 1. Design Philosophy

During high-severity incidents, the infrastructure supporting monitoring and alerting often experiences failures, timeouts, and throttling. Standard LLM agents that make synchronous, unguarded HTTP calls or rely on brittle output parsing fail catastrophically in these conditions.

`resilient-triage` is engineered on four core architectural pillars:
1. **Never stall on dead dependencies**: Use circuit breakers with fail-fast degraded fallbacks.
2. **Absorb transient turbulence**: Wrap external telemetry and model calls with exponential backoff and randomized jitter.
3. **Guarantee structural determinism**: Strictly enforce Pydantic contracts with an automated self-repair loop that catches invalid outputs before they reach engineers.
4. **Sub-20ms Semantic Caching with Zero-Downtime Failover**: Offload redundant incident traffic using Redis 8 RediSearch, with instantaneous fallback to in-memory vectorized search if Redis disconnects.

---

## 2. System Architecture & Component Interactions

<p align="center">
  <a href="architecture-diagram.html">
    <img src="architecture-diagram.svg" alt="Resilient Incident Triage Architecture" width="100%">
  </a>
</p>
<p align="center">
  <em>Figure 1: Full-system topology showing ingress routing, dual-engine semantic caching highway (Redis 8 & NumPy fallback), circuit-breaker guarded telemetry probes, and strict Pydantic validation. <a href="architecture-diagram.html">Open interactive view &rarr;</a></em>
</p>

---

## 3. Deep-Dive Components

### 1. Redis 8 RediSearch Semantic Caching
- **Purpose**: Prevent redundant LLM evaluations and downstream API calls during an ongoing incident when multiple engineers ask similar questions.
- **Mechanism**:
  - Ingress queries are embedded into a dense 384-dimensional vector in ~3.5ms via FastEmbed ONNX runtime (`BAAI/bge-small-en-v1.5`).
  - RediSearch performs KNN similarity search using an HNSW Float32 Cosine distance index (`triage_vector_idx`).
  - If cosine similarity $\ge 0.90$, the cached `IncidentTriageReport` is immediately returned in **`7.71ms`** with header `X-Cache: HIT`.
  - Cache misses proceed to the LangGraph state machine, and the final validated report is stored in Redis under `triage:doc:*` with scope and TTL management (300s healthy, 60s degraded).
- **Zero-Downtime Resilience**:
  - If Redis disconnects or crashes mid-traffic, `SemanticCacheManager` intercepts the socket error, logs a warning, and immediately serves from an in-memory NumPy vector store with **0 dropped requests and 0 HTTP 500 errors**.
  - Non-blocking background probes automatically restore RediSearch when Redis recovers without requiring a server restart.

---

### 2. SOTA LLM Engine (Groq LPUs)
- **Model**: `qwen/qwen3.8-27b` (Flagship 27-Billion parameter SRE model).
- **Latency**: **~1.1s** generation turnaround (166ms TTFT).
- **Protocol**: Direct async HTTP client with strict `response_format={"type": "json_object"}`.
- **Provider Ladder**:
  1. Groq LPUs (`qwen/qwen3.8-27b`, `openai/gpt-oss-120b`).
  2. Local Apple Silicon GPU via Ollama (`qwen2.5:1.5b`).
  3. Google Gemini 2.5 Flash.
  4. OpenAI GPT-4o-mini.
  5. High-fidelity zero-config SRE simulation fallback.

---

### 3. Resilience Layer: Tenacity Retries
- **Purpose**: Absorb transient networking hiccups, intermittent 503s, and HTTP 429 rate limits from telemetry and model providers.
- **Parameters**:
  - Retry on `httpx.TimeoutException`, `httpx.NetworkError`, and specific HTTP status codes (429, 502, 503, 504).
  - Exponential backoff with randomized jitter (`wait_random_exponential`) to prevent thundering herd problems against upstream services.

---

### 4. Resilience Layer: Pybreaker Circuit Breakers
- **Purpose**: Prevent cascades of hanging requests when an upstream provider (such as an external Statuspage or an internal telemetry endpoint) is completely offline.
- **State Machine**:
  - `CLOSED`: Normal operation. Calls pass through.
  - `OPEN`: If 3 consecutive failures occur (`fail_max=3`), the breaker trips `OPEN`. All subsequent calls fail immediately with `pybreaker.CircuitBrokenError` without placing network load.
  - `HALF_OPEN`: After a reset timeout (`reset_timeout=30s`), a single probe call is allowed through. Success resets to `CLOSED`; failure immediately re-trips to `OPEN`.
- **Graph Routing**: When a breaker trips open, the LangGraph state machine intercepts the exception and steers execution to the `DegradedFallbackNode` instead of crashing.

---

### 5. LangGraph Cyclic State Machine Topology

<p align="center">
  <a href="state-machine-diagram.html">
    <img src="state-machine-diagram.svg" alt="LangGraph State Machine & Resilience Lifecycle" width="100%">
  </a>
</p>
<p align="center">
  <em>Figure 2: LangGraph cyclic state machine lifecycle showing the primary execution rail, adaptive recovery detours (blindspot compensation & self-repair), and terminal resolution outcomes. <a href="state-machine-diagram.html">Open interactive view &rarr;</a></em>
</p>

- **`analyze_query`**: Analyzes incident keywords and service scope to dynamically target telemetry tools (`statuspage`, `chaos`, or both).
- **`collect_telemetry`**: Concurrently fetches telemetry through Tenacity retry wrappers and Pybreaker circuit breakers using `asyncio.gather(..., return_exceptions=True)`.
- **`degraded_fallback`**: Invoked if any tripped breaker was recorded (a targeted tool whose circuit breaker is `OPEN`). Annotates the incident with known blind spots and synthesizes available telemetry.
- **`synthesize`**: Merges telemetry signals and historical context.
- **`generate_report`**: Emits a structured candidate JSON triage report via the active LLM engine.
- **`validate_report`**: Markdown fence auto-cleaning (`clean_json_string`) and strict Pydantic validation. On failure, passes exact error paths to `self_repair`.
- **`self_repair`**: Feeds `ValidationError` details back to the LLM context to correct malformed fields (max 2 retries).
- **`fallback_report`**: Constructs a deterministic safe report if retries are exhausted.
- **`finalize`**: Runs **report reconciliation** (`reconcile` in `graph/reconciliation.py`) on the report, whichever path produced it, then commits it to the semantic cache. It is one of the two call sites that set a report's `degradation_status` and `circuit_breakers_tripped`; the other is the API's emergency path (below).

### Report reconciliation

`collect_telemetry` records `tripped_breakers` in state: the *targeted* tools whose breaker is `OPEN` at collection time. An open breaker the query never targets does not count. It also records `unavailable_sources`: targeted tools whose fetch raised or returned an `UNAVAILABLE` signal while their breaker was not tripped. `fallback_report` records `synthesis_fell_back: True`. `finalize` builds a `DegradationEvidence` from that state and calls the pure `reconcile(report, evidence)`, which returns a copy with:

- `circuit_breakers_tripped` = the sorted tripped-breaker set. Whatever the LLM wrote is discarded.
- `degradation_status` = the maximum (HEALTHY < PARTIALLY_DEGRADED < SEVERELY_DEGRADED) of: synthesis fallback ⇒ SEVERELY_DEGRADED; two or more tripped breakers ⇒ SEVERELY_DEGRADED; exactly one ⇒ PARTIALLY_DEGRADED; any unavailable source ⇒ PARTIALLY_DEGRADED; otherwise HEALTHY. The LLM's value is discarded and `UNKNOWN` is never produced.

**Approval rule.** `reconcile` also sets `requires_approval=True` on every recommended action whose `action_type.strip().lower()` is not `"diagnostic"`, so an unexpected label such as `"Remediation"` or `"mutating"` fails safe. The rule only raises the flag, never lowers it, and is the only code that writes `requires_approval`; `validate_report` just parses and fills defaults.

**Second call site: the emergency path.** If the graph returns no `final_report`, `POST /triage` in `api/app.py` builds one with `fallback_report_node` directly, never reaching `finalize`. It then calls `reconcile` itself, on the graph's final state merged with the fallback's `synthesis_fell_back: True`, before caching and returning, so that report is `SEVERELY_DEGRADED` and lists any tripped targeted breakers.

---

### 6. Dual-Mode Self-Booting Container Packaging
- **`Dockerfile`**: Packages `redis-server` and `redis-tools` directly inside the Python 3.12 slim container.
- **`scripts/docker-entrypoint.sh`**:
  - **Single-Container Deployments** (Hugging Face Spaces, Render Free, Railway): Automatically boots an embedded background Redis daemon with `maxmemory 128mb` and `allkeys-lru` eviction.
  - **External Database Override**: If `REDIS_URL` points to an external cloud database (Upstash, AWS ElastiCache, or Docker Compose), internal Redis is skipped and it connects directly.

---

### 7. High-Performance Latency & Observability Optimizations (v1.3.0)

During real-world cloud deployment evaluations (e.g. Render Singapore), external network transit, upstream API calls, and cold ONNX runs were identified and eliminated:

1. **Statuspage 10-Second Short-TTL Cache**:
   - Upstream Atlassian/GitHub status pages update every 1–5 minutes.
   - `StatuspageClient` caches parsed summaries for 10 seconds in-memory, eliminating transcontinental HTTPS latency (**~650ms – 700ms saved per query**).
2. **Persistent Groq HTTP Connection Pooling & Token Budget**:
   - Replaced per-request HTTP client creation with a persistent, keep-alive client pool (`httpx.Limits(max_keepalive_connections=20, max_connections=50)`).
   - Added `max_tokens=650` to the Groq LPU payload to eliminate runaway token generation while producing complete, structured incident reports.
   - Eliminates ~200ms of TCP/TLS handshake latency and ~300ms of generation time.
3. **In-Memory LRU Vector Cache (512 Entries) & Preset Pre-Warming**:
   - Added an `OrderedDict` LRU cache to both `FastEmbedProvider` and `DeterministicEmbeddingProvider`.
   - All quick presets (`Actions 503`, `Stripe Timeout`, `Postgres Pool`) are pre-warmed during FastAPI startup (`lifespan`).
   - Lookups, stores, and preset re-runs execute in **0.001ms** instead of 100–250ms on shared vCPU environments.
4. **Responsive Dual-Metric Observability**:
   - The SRE Command Center UI separates **Server Execution Time** (`data.execution_time_ms`) from **Internet WAN Network RTT** (`elapsed - serverMs`).
   - Recruiter/reviewer consoles display: `Server: 1.16ms (Redis HNSW) • Net RTT: 78ms (79ms total)`.
   - Audit log ticker records `POST /triage 200 (Srv: 1ms | Net: 78ms)`.

---

### 8. Real-Time Groq Quota Monitoring & Model Failover (v1.2.0)
- **Problem**: Groq free tier imposes a strict 1,000 output tokens/minute limit on `qwen/qwen3.8-27b`. Bursts of incident triage requests can hit HTTP 429 mid-incident.
- **Header Telemetry (`GroqQuotaTracker`)**:
  - Thread-safe singleton inspects response headers (`x-ratelimit-remaining-tokens`, `x-ratelimit-limit-tokens`, `x-ratelimit-reset-tokens`, etc.).
  - Tracks rolling metrics: total calls, 429 rate limit hits, and fallback invocations.
  - Returns strict typed snapshot [`LLMQuotaSnapshot`](schemas.md#llmquotasnapshot) via `GET /llm/quota` and embeds into `GET /health`.
- **Secondary Model Failover**:
  - On HTTP 429, `GroqChatModel` immediately falls back to `openai/gpt-oss-20b`, which possesses an independent quota bucket.
  - SRE incident response continues without interruptions or gateway timeouts.
- **Diagnostic Logging**:
  - Fallback errors log exact HTTP status codes and full response body (`fb_resp.text`) for rapid upstream root-cause diagnosis.

---

### 9. Unified Interactive State Management
- **Demo State Reset**: `resetDemoState(full=true)` restores circuit breakers, clears chaos injections, resets visual topology cards, wipes previous report cards, clears thread IDs, and purges the live audit stream.
- **Fine-Tuned Stories**:
  - **Story 1 (Cold Triage)**: Enforces `force_refresh=True` on Gateway 503 incident to demonstrate live AI synthesis.
  - **Story 2 (Instant Semantic Recall)**: Submits synonymous query (`Internal API gateway cluster...`), achieving **94.2% cosine similarity hit** in $<20\text{ms}$.
  - **Story 3 (Chaos Outage & Fallback)**: Injects 503s, trips circuit breaker to `OPEN`, and forces live degraded routing with circuit breaker annotation.

