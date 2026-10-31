# REST API & OpenAPI Specification

The `resilient-triage` gateway exposes a high-performance REST API built on FastAPI 0.110+. All endpoints feature non-blocking asynchronous execution, correlation tracking, and execution time headers.

- **Base URL**: `http://127.0.0.1:8000` (Local) / Custom Cloud Domain
- **OpenAPI Swagger UI**: `/docs`
- **ReDoc UI**: `/redoc`

---

## Global Response Headers

Every HTTP response returned by the API includes the following observability headers:

| Header | Description | Example |
| :--- | :--- | :--- |
| `x-request-id` | Unique UUID correlation identifier for request tracing across logs | `550e8400-e29b-41d4-a716-446655440000` |
| `x-process-time-ms` | Total server processing time in milliseconds | `12.45` |
| `x-cache` | Cache status (`HIT` or `MISS`) — *present on `/triage`* | `HIT` |
| `x-cache-similarity` | Cosine similarity score ($\ge 0.90$) — *present on cache `HIT`* | `0.9417` |
| `x-thread-id` | Correlation id: the caller's `X-Thread-ID` echoed back, or a new UUID4 if none was sent. It names no checkpoint thread (ADR-007) — *present on `/triage`* | `e1f8b4a2-...` |

### Security & Authentication Headers
- `X-Admin-Key`: Required on the mutating admin endpoints (`/resilience/reset`, `/cache/clear`, `/chaos/inject`). They fail closed:
  - `ADMIN_API_KEY` unset or empty on the server: `403 Forbidden` for every caller, whatever header it sends.
  - `ADMIN_API_KEY` set: a missing or wrong header gets `401 Unauthorized`; the matching key (compared in constant time) is admitted.
- The header is declared as an OpenAPI `apiKey` security scheme (`AdminKey`), so `/docs` has an **Authorize** button: enter the key there once and Swagger UI sends it on the admin routes. That is the operator path. Admin routes act only on the shared runtime.

---

## API Endpoints Summary

| Method | Endpoint | Description | Auth |
| :--- | :--- | :--- | :---: |
| `POST` | [`/triage`](#1-post-triage) | Run autonomous incident triage analysis | Public |
| `POST` | [`/triage/stream`](#1a-post-triagestream) | Same triage, streaming its execution trace as Server-Sent Events | Public |
| `GET` | [`/scenarios`](#1b-get-scenarios) | The scenario catalogue's public view, by chapter in display order | Public |
| `POST` | [`/scenarios/{scenario_id}/stream`](#1c-post-scenariosscenario_idstream) | Run one catalogued scenario on private collaborators, streaming its trace | Public |
| `GET` | [`/health`](#2-get-health) | Check system availability, active model, and circuit states | Public |
| `GET` | [`/resilience/status`](#3-get-resiliencestatus) | Detailed diagnostics for breakers, chaos, and cache | Public |
| `GET` | [`/llm/quota`](#4-get-llmquota) | Retrieve real-time Groq rate limits and token budgets | Public |
| `GET` | [`/metrics`](#5-get-metrics) | Prometheus exposition metrics for scrape collectors | Public |
| `POST` | [`/resilience/reset`](#6-post-resiliencereset) | Reset all circuit breakers, chaos state, and counters | Admin (`X-Admin-Key`) |
| `POST` | [`/chaos/inject`](#7-post-chaosinject) | Configure runtime fault injection parameters | Admin (`X-Admin-Key`) |
| `GET` | [`/chaos/config`](#8-get-chaosconfig) | Retrieve active runtime chaos simulation configuration | Public |
| `GET` | [`/chaos/probe`](#9-get-chaosprobe) | Upstream probe target to test circuit tripping | Public |
| `POST` | [`/cache/clear`](#10-post-cacheclear) | Flush Redis and in-memory semantic cache | Admin (`X-Admin-Key`) |

---

## Endpoint Details

### 1. `POST /triage`
Executes autonomous incident triage. Checks Redis semantic cache for similarity $\ge 0.90$. If miss, dispatches LangGraph cyclic state machine.

> [!NOTE]
> **Concurrency Load Shedding**: The Triage gateway bounds graph runs to `TRIAGE_MAX_CONCURRENT_RUNS` (default 10) per app. A request that waits longer than `TRIAGE_QUEUE_TIMEOUT_S` (default 5.0s) for a run slot is shed with `HTTP 503 Service Unavailable`.
>
> **Free-play run budget**: at most `FREE_PLAY_RUNS_PER_MINUTE` (default 20) graph runs are admitted in any sliding 60 s window, per process (per worker). Only graph runs count: a cache miss or a `force_refresh` request, counted once admitted even if it is then shed for lack of a slot. Cache hits are always free, and scenario runs never count. A run over the budget is shed at once (no queueing) with `HTTP 503` and a `Retry-After` header: whole seconds (at least 1) until the oldest counted run leaves the window. A shed for lack of a slot has no `Retry-After`. Both count as `saturated` on `/metrics`. `POST /triage/stream` sends no `Retry-After` header (its 200 headers go out before the budget is checked): there, the retry-after is the `run_slot` event's `retry_after_s`, and the stream still ends with `trace_finished(outcome=shed)`.

#### Request Body (`application/json`)
```json
{
  "query": "Internal API gateway service failing with 503 outage and elevated probe latency",
  "service_filter": "internal",
  "force_refresh": false
}
```

| Field | Type | Required | Description |
| :--- | :--- | :---: | :--- |
| `query` | `string` | **Yes** | Natural language description of symptoms, errors, or affected microservices (3–2000 chars). |
| `service_filter` | `string \| null` | No | Optional scope filter (alphanumeric, underscores, hyphens, max 100 chars, e.g., `"internal"`, `"billing"`, `"database"`). |
| `force_refresh` | `boolean` | No | If `true`, bypasses the semantic cache and executes live AI inference. Default: `false`. |

#### Response (`200 OK`)
```json
{
  "report": {
    "incident_id": "947bb37f-ecb2-4d2a-a92c-567f8b91cbb1",
    "timestamp": "2026-10-10T16:42:11.950419Z",
    "summary": "Internal API gateway is reporting elevated 503 errors and high probe latency.",
    "severity": "HIGH",
    "root_cause_analysis": "Upstream microservices behind the API gateway are saturating connection pools.",
    "affected_services": [
      {
        "service_name": "api-gateway",
        "impact_level": "HIGH",
        "details": "Gateway ingress rejecting 12% of incoming traffic with 503 Service Unavailable."
      }
    ],
    "telemetry_signals": [
      {
        "source": "chaos_probe",
        "status": "OK",
        "latency_ms": 0.39,
        "error_rate": 0.0,
        "raw_snippet": "All internal systems and chaos telemetry operational."
      }
    ],
    "recommended_actions": [
      {
        "priority": "P0",
        "action": "Scale gateway ingress replicas from 4 to 12 via kubectl scale deployment api-gateway --replicas=12",
        "target_system": "api-gateway",
        "action_type": "remediative",
        "requires_approval": true
      }
    ],
    "degradation_status": "HEALTHY",
    "circuit_breakers_tripped": [],
    "confidence_score": 0.85
  },
  "cached": false,
  "cache_similarity": null,
  "execution_time_ms": 1296.74,
  "trace": {
    "scenario_id": null,
    "outcome": "analyzed",
    "nodes": [
      {"node": "analyze_query", "duration_ms": 0.014, "status": "ok"},
      {"node": "collect_telemetry", "duration_ms": 182.4, "status": "ok"},
      {"node": "synthesize_telemetry", "duration_ms": 0.018, "status": "ok"},
      {"node": "generate_report", "duration_ms": 1090.2, "status": "ok"},
      {"node": "validate_report", "duration_ms": 0.041, "status": "ok"},
      {"node": "finalize_report", "duration_ms": 0.026, "status": "ok"}
    ],
    "routes": ["synthesize", "validate", "finalize"],
    "model_calls": [
      {"model": "qwen/qwen3.8-27b", "role": "primary", "outcome": "rate_limited", "duration_ms": 41.0},
      {"model": "llama-3.1-8b-instant", "role": "fallback", "outcome": "completion", "duration_ms": 1048.9}
    ],
    "failovers": 1,
    "telemetry": [{"source": "chaos", "outcome": "read", "attempts": 1}],
    "repair_rounds": 0,
    "refine_rounds": 0,
    "reconciliation": {
      "degradation_before": "HEALTHY", "degradation_after": "HEALTHY",
      "breakers_changed": false, "breakers_after": [], "approval_raised": [0]
    },
    "degradation": "HEALTHY",
    "deadline_exceeded": false,
    "timings": {
      "total_ms": 1296.5, "cache_lookup_ms": 4.1, "slot_wait_ms": 0.007,
      "run_ms": 1281.3, "model_ms": 1089.9
    }
  }
}
```

`trace` is the request's **trace summary** (`TraceSummary`, ADR-008): a small, structured digest of its execution trace, never the event list and never free text (every string is an id or a fixed value). It is `summarize()` of the events the Triage gateway recorded before `trace_finished`:

| Field | Meaning |
| :--- | :--- |
| `scenario_id` | Catalogue scenario id for a scenario run; `null` for `POST /triage`. |
| `outcome` | `cache_hit` or `analyzed` (a shed request gets a 503, with no body summary). |
| `nodes` | Graph node visits in order, each with `duration_ms` and `status` (`ok` / `error` / `cancelled`). Empty on a cache hit. |
| `routes` | Router keys taken, in order (e.g. `compensate`, `repair`, `refine`, `finalize`). |
| `model_calls` | One entry per model attempt: `model`, `role` (`primary` / `fallback`), `outcome`, `duration_ms`. |
| `failovers` | Calls made to the fallback model, failed ones included. |
| `telemetry` | One entry per run-time source read: `source`, `outcome` (`read` / `tripped` / `unavailable`), `attempts`. |
| `repair_rounds`, `refine_rounds` | Visits to `self_repair` and `refine_reflection`. |
| `reconciliation` | What report reconciliation changed (`degradation_before`/`after`, `breakers_changed`, `breakers_after`, `approval_raised` action indexes). `null` on a cache hit. |
| `degradation` | The answer's reconciled degradation status; `null` on a cache hit (the cached report carries its own). |
| `deadline_exceeded` | `true` when the run deadline passed and a synthesis fallback answered. |
| `timings` | Milliseconds from `trace_started`, rounded to microseconds: `total_ms` (to the last event before `trace_finished`), `cache_lookup_ms`, `slot_wait_ms`, `run_ms` (first node start to last node finish), `model_ms` (sum of model calls). `null` where the step did not happen. |

#### Execution trace events
The summary is derived from the request's trace events. `POST /triage` returns only the summary; the event stream itself is for [`POST /triage/stream`](#1a-post-triagestream), where every stream ends in `trace_finished` carrying the same `TriageResponse` this route returns. Every event has `kind`, `seq` (0, 1, …) and `t_ms` (monotonic ms since `trace_started`, which is stamped 0; a scenario run's `scenario_setup` steps and setup `telemetry_read`s come before it and are stamped 0 too). A cache hit is `trace_started`, `cache_lookup`, `trace_finished`; a run deadline records `deadline_exceeded`, then the reconciled fallback (`reconciled`), `cache_store` (`skipped_fallback`) and `trace_finished`.

| `kind` | Fields (besides `seq`, `t_ms`) |
|---|---|
| `scenario_setup` | `step` (`seed_cache` / `hold_run_slots` / `fail_source` / `set_run_deadline`), `count`, `source` (`fail_source`), `deadline_s` (`set_run_deadline`) |
| `trace_started` | `schema_version` (1), `scenario_id`, `scoped`, `force_refresh` |
| `cache_lookup` | `backend`, `hit`, `similarity`, `threshold`, `duration_ms` |
| `run_slot` | `acquired`, `waited_ms`, `capacity`, `reason` (`capacity` / `budget` when shed, else none), `retry_after_s` (budget sheds only) |
| `node_started` | `node` |
| `node_finished` | `node`, `duration_ms`, `status` (`ok` / `error` / `cancelled`), `confidence` (validate_report only) |
| `route_decided` | `source`, `target`, `route` (the router key) |
| `model_call` | `model`, `role` (`primary` / `fallback`), `outcome`, `rate_limit_scope`, `retry_after_s`, `duration_ms` |
| `telemetry_read` | `source`, `phase` (`setup` or none), `outcome` (`read` / `tripped` / `unavailable`), `attempts`, `breaker_before`, `breaker_after`, `signals`, `duration_ms` |
| `validation_failed` | `attempt`, `error_count`, `fields` (declared report fields), `error_types` (Pydantic types) |
| `reconciled` | `changes`: `degradation_before`/`after`, `breakers_changed`, `breakers_after`, `approval_raised` (action indexes) |
| `deadline_exceeded` | `deadline_s` |
| `cache_store` | `backend`, `outcome` (`stored` / `skipped_fallback` / `failed`), `duration_ms` |
| `trace_finished` | `outcome` (`cache_hit` / `analyzed` / `shed`), `response` (`TriageResponse`, none when shed) |

#### Example cURL
```bash
# Standard query (Checks semantic cache first)
curl -X POST http://127.0.0.1:8000/triage \
  -H "Content-Type: application/json" \
  -d '{"query": "Gateway service reporting 503 errors"}'

# Force live LLM generation (Bypasses cache)
curl -X POST http://127.0.0.1:8000/triage \
  -H "Content-Type: application/json" \
  -d '{"query": "Gateway service reporting 503 errors", "force_refresh": true}'
```

---

### 1a. `POST /triage/stream`
Runs the same triage as `POST /triage` (same `TriageRequest` body, same cache, run slots, run deadline and fallbacks) and streams its [execution trace events](#execution-trace-events) as Server-Sent Events while it runs (ADR-008). A browser reads it with `fetch()` and a stream parser: `EventSource` cannot send a POST body.

#### Response (`200 OK` - `text/event-stream`)
Each trace event is one SSE event: `event:` is its `kind` and `data:` is the event as JSON. While nothing happens the server sends a `: ping` comment line every 15 s; skip comment lines. Responses carry `Cache-Control: no-cache` and `X-Accel-Buffering: no`.
```text
event: trace_started
data: {"kind":"trace_started","seq":0,"t_ms":0.0,"schema_version":1,"scenario_id":null,"scoped":false,"force_refresh":false}

event: cache_lookup
data: {"kind":"cache_lookup","seq":1,"t_ms":4.1,"backend":"in_memory","hit":false,"similarity":null,"threshold":0.85,"duration_ms":4.0}

: ping

event: trace_finished
data: {"kind":"trace_finished","seq":41,"t_ms":2150.7,"outcome":"analyzed","response":{"report":{...},"cached":false,"cache_similarity":null,"execution_time_ms":2150.6,"trace":{...}}}
```

- **Exactly one `trace_finished`, always last.** Its `response` is the `TriageResponse` that `POST /triage` would return for the same request (none when `outcome` is `shed`). The status is 200 once the stream starts, so failures arrive inside it, never as an HTTP error: a shed request ends `run_slot(acquired=false, reason=capacity|budget)`, `trace_finished(outcome=shed)` (where `POST /triage` answers 503; a budget shed's `run_slot` carries the `retry_after_s` the 503 sends as `Retry-After`; the stream itself has no `Retry-After` header, so a client reads it from that event); a run deadline records `deadline_exceeded` before the fallback; an unexpected exception ends the stream with a `SEVERELY_DEGRADED` synthesis fallback in `trace_finished` (`outcome=analyzed`, not cached). A request body that fails validation is still a 422 before any stream starts.
- **No headers, no ids, no resume.** The cache outcome is in `trace_finished` (`outcome`, `response.cached`), not in `X-Cache` headers. There is no run id, no `GET` and no `Last-Event-ID` resume: a dropped connection loses its trace, and re-sending the request may now be a cache hit.
- **A client disconnect cancels the run.** The run's slot is released, nothing is cached (a cancel that lands during the cache store evicts the entry again), and the request counts once, whenever the disconnect lands, in `resilient_triage_abandoned_requests_total` on [`/metrics`](#5-get-metrics), not as a hit, miss or saturated request.

#### Example cURL
```bash
curl -N -X POST http://127.0.0.1:8000/triage/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "Gateway service reporting 503 errors"}'
```

---

### 1b. `GET /scenarios`
The scenario catalogue's public view (ADR-009): every chapter in display order, each with its scenarios in display order. A scenario's entry is its page copy only (`ScenarioView`), never its query, script or expected outcome. The catalogue is fixed in code, so the response only changes with a deploy.

#### Response (`200 OK`) - `ScenarioCatalogue`
```json
{
  "chapters": [
    {
      "id": "fast-paths",
      "title": "Fast paths",
      "scenarios": [
        {
          "id": "healthy-run",
          "chapter": "fast-paths",
          "title": "...",
          "caption": "...",
          "setup_notes": []
        }
      ]
    }
  ]
}
```

| Field | Meaning |
| :--- | :--- |
| `chapters[].id`, `chapters[].title` | The chapter's id and heading (`fast-paths`, `model-misbehaves`, `dependencies-fail`, `finale`). |
| `chapters[].scenarios[]` | `id` (what `POST /scenarios/{scenario_id}/stream` takes), `chapter` (the chapter's id), `title`, `caption`, `setup_notes` (what is true before the run, in words). |

---

### 1c. `POST /scenarios/{scenario_id}/stream`
Runs one catalogued scenario and streams its execution trace exactly like [`POST /triage/stream`](#1a-post-triagestream): same SSE framing and pings, exactly one `trace_finished`, always last; a disconnect cancels the run. The body is empty: the scenario fixes the query, scope, the model's replies and every fault. An unknown `scenario_id` is a `404` before any stream starts.

- **Setup first.** The scenario setup (`scenario_setup` events, and setup `telemetry_read`s with `phase: "setup"`) streams live before `trace_started`, whose `scenario_id` names the scenario; setup events are stamped `t_ms` 0, so the run's clock starts at `trace_started`. A setup that fails ends the stream with an uncached `SEVERELY_DEGRADED` synthesis fallback in `trace_finished`, as for any unexpected exception.
- **Private collaborators.** Each run gets its own scripted model gateway, telemetry sources, breakers, chaos manager and in-memory cache, so scenario runs never change `/metrics` outcome counts, `/llm/quota`, `/resilience/status` or `/health`, and never read or write the shared cache.
- **Own slot pool.** Scenario runs share a pool of `SCENARIO_MAX_CONCURRENT_RUNS` (default 4) run slots, separate from free play's, and wait up to `TRIAGE_QUEUE_TIMEOUT_S` for one; free play and scenario traffic can't shed each other. A scenario shed for lack of a slot still answers 200 and ends `trace_finished(outcome=shed)`. `shed-request` holds every slot of a private pool, so it is always shed and never touches the shared one. The free-play run budget does not apply.
- **Real time.** Scripted replies take about 0.9 s and failures about 0.18 s; `run-deadline` waits about 3 s for its deadline and `shed-request` about 1 s in the queue.

#### Example cURL
```bash
curl -N -X POST http://127.0.0.1:8000/scenarios/self-repair/stream
```

---

### 2. `GET /health`
Liveness and readiness health probe for Kubernetes/load balancers, returning service availability, Redis connection driver, and circuit breaker states.

#### Response (`200 OK`)
```json
{
  "status": "healthy",
  "version": "2.0.0",
  "redis_connected": true,
  "cache_driver": "redisearch",
  "circuits": {
    "statuspage": "closed",
    "chaos": "closed"
  },
  "active_model": "Groq LPU (qwen/qwen3.8-27b)",
  "llm_quota": {
    "provider": "groq",
    "active_model": "qwen/qwen3.8-27b",
    "fallback_model": "llama-3.1-8b-instant",
    "remaining_requests": 999,
    "limit_requests": 1000,
    "remaining_tokens": 7112,
    "limit_tokens": 8000,
    "reset_requests": "1m26.4s",
    "reset_tokens": "6.66s",
    "total_calls": 1,
    "rate_limit_hits": 0,
    "failover_events": 0,
    "last_status": 200,
    "last_error": null,
    "last_updated": "2026-10-10T16:36:12.276663Z"
  }
}
```

#### Example cURL
```bash
curl -s http://127.0.0.1:8000/health
```

---

### 3. `GET /resilience/status`
Detailed SRE diagnostic snapshot covering circuit breaker state machines, chaos injection flags, active cache driver, and LLM quota telemetry.

#### Response (`200 OK`)
```json
{
  "circuits": {
    "statuspage": {
      "name": "statuspage",
      "state": "closed",
      "fail_counter": 0,
      "fail_max": 3,
      "is_open": false,
      "last_failure_time": null,
      "last_state_change": "2026-10-10T16:00:00Z",
      "last_failure_reason": null
    },
    "chaos": {
      "name": "chaos",
      "state": "closed",
      "fail_counter": 0,
      "fail_max": 3,
      "is_open": false,
      "last_failure_time": null,
      "last_state_change": "2026-10-10T16:00:00Z",
      "last_failure_reason": null
    }
  },
  "chaos_active": false,
  "cache_driver": "redisearch",
  "cache_entries_count": 4,
  "active_model": "Groq LPU (qwen/qwen3.8-27b)",
  "llm_quota": { ... }
}
```

#### Example cURL
```bash
curl -s http://127.0.0.1:8000/resilience/status
```

---

### 4. `GET /llm/quota`
Returns the LLM gateway's quota snapshot: the primary model's rate-limit budgets from Groq's response headers, plus call, rate-limit and failover counters for both models. `active_model` is the model that served the last completion. Without `GROQ_API_KEY` the budget fields stay `null`. Field reference: [`LLMQuotaSnapshot`](schemas.md#llmquotasnapshot).

#### Response (`200 OK`)
```json
{
  "provider": "groq",
  "active_model": "qwen/qwen3.8-27b",
  "fallback_model": "llama-3.1-8b-instant",
  "remaining_requests": 999,
  "limit_requests": 1000,
  "remaining_tokens": 7112,
  "limit_tokens": 8000,
  "reset_requests": "1m26.4s",
  "reset_tokens": "6.66s",
  "total_calls": 1,
  "rate_limit_hits": 0,
  "failover_events": 0,
  "last_status": 200,
  "last_error": null,
  "last_updated": "2026-10-10T16:36:12.276663Z"
}
```

#### Example cURL
```bash
curl -s http://127.0.0.1:8000/llm/quota
```

---

### 5. `GET /metrics`
Prometheus text exposition format endpoint for scraping operational metrics, cache hit counters, circuit breaker states, and LLM rate-limit events.

#### Response (`200 OK` - `text/plain; version=0.0.4`)
```text
# HELP resilient_triage_requests_total Total incident triage requests handled by gateway
# TYPE resilient_triage_requests_total counter
resilient_triage_requests_total{cache="hit"} 42
resilient_triage_requests_total{cache="miss"} 8
resilient_triage_requests_total{cache="saturated"} 0

# HELP resilient_triage_abandoned_requests_total Triage requests cancelled before they ended, e.g. by a client disconnect (not a triage outcome)
# TYPE resilient_triage_abandoned_requests_total counter
resilient_triage_abandoned_requests_total 1

# HELP resilient_triage_circuit_breaker_state Active circuit breaker state (1 if active, 0 otherwise)
# TYPE resilient_triage_circuit_breaker_state gauge
resilient_triage_circuit_breaker_state{name="statuspage",state="closed"} 1
resilient_triage_circuit_breaker_state{name="statuspage",state="open"} 0
resilient_triage_circuit_breaker_state{name="statuspage",state="half_open"} 0
resilient_triage_circuit_breaker_state{name="chaos",state="closed"} 1
resilient_triage_circuit_breaker_state{name="chaos",state="open"} 0
resilient_triage_circuit_breaker_state{name="chaos",state="half_open"} 0

# HELP resilient_triage_circuit_breaker_fail_counter Current consecutive failure count
# TYPE resilient_triage_circuit_breaker_fail_counter gauge
resilient_triage_circuit_breaker_fail_counter{name="statuspage"} 0
resilient_triage_circuit_breaker_fail_counter{name="chaos"} 0

# HELP resilient_triage_cache_entries Total incident reports persist in semantic cache
# TYPE resilient_triage_cache_entries gauge
resilient_triage_cache_entries{driver="redisearch"} 12

# HELP resilient_triage_llm_rate_limit_hits_total Total HTTP 429 rate limit events encountered
# TYPE resilient_triage_llm_rate_limit_hits_total counter
resilient_triage_llm_rate_limit_hits_total 0

# HELP resilient_triage_llm_failovers_total Total fallback model invocations triggered
# TYPE resilient_triage_llm_failovers_total counter
resilient_triage_llm_failovers_total 0
```

#### Example cURL
```bash
curl -s http://127.0.0.1:8000/metrics
```

---

### 6. `POST /resilience/reset`
Administrative reset restoring all circuit breakers to `CLOSED`, clearing chaos injection rules, and resetting the LLM gateway: its counters and last error are zeroed and every model cooldown ends.

#### Headers
| Header | Type | Description |
| :--- | :--- | :--- |
| `X-Admin-Key` | `string` | **Required.** Must equal the server's `ADMIN_API_KEY` (`401` if missing or wrong; `403` if the server has no key configured). |

#### Response (`200 OK`)
```json
{
  "status": "ok",
  "message": "Circuit breakers, chaos state, and LLM counters successfully reset"
}
```

#### Example cURL
```bash
curl -X POST http://127.0.0.1:8000/resilience/reset \
  -H "X-Admin-Key: secret-admin-key"
```

---

### 7. `POST /chaos/inject`
Configures live chaos injection parameters to simulate upstream infrastructure failures.

#### Headers
| Header | Type | Description |
| :--- | :--- | :--- |
| `X-Admin-Key` | `string` | **Required.** Must equal the server's `ADMIN_API_KEY` (`401` if missing or wrong; `403` if the server has no key configured). |

#### Request Body (`application/json`)
```json
{
  "active": true,
  "inject_503": true,
  "failure_rate": 1.0,
  "latency_ms": 250
}
```

| Field | Type | Description |
| :--- | :--- | :--- |
| `active` | `boolean` | Master enable toggle for chaos injection. |
| `inject_503` | `boolean` | If `true`, probe immediately returns HTTP 503. |
| `failure_rate` | `float` | Probability of injected failure ($0.0 \le p \le 1.0$). |
| `latency_ms` | `integer` | Artificial lag to introduce in milliseconds (0 to 30 000; above that the request is rejected with `422`). A probe slower than the source deadline (`TELEMETRY_SOURCE_DEADLINE_S`) is cut off and counts as a failure. |

#### Response (`200 OK`)
```json
{
  "active": true,
  "inject_503": true,
  "failure_rate": 1.0,
  "latency_ms": 250
}
```

---

### 8. `GET /chaos/config`
Retrieves currently active runtime chaos configuration settings.

#### Response (`200 OK`)
```json
{
  "active": false,
  "inject_503": false,
  "failure_rate": 0.0,
  "latency_ms": 0
}
```

#### Example cURL
```bash
curl -s http://127.0.0.1:8000/chaos/config
```

---

### 9. `GET /chaos/probe`
Health probe target simulated under chaos injection rules. Calling this endpoint when `inject_503: true` returns HTTP 503, used to trip circuit breakers.

#### Response
- **Healthy (`200 OK`)**:
  ```json
  {
    "status": "OK",
    "latency_injected_ms": 0,
    "error_injected": false,
    "details": "All internal systems and chaos telemetry operational.",
    "timestamp": "2026-10-10T16:42:11.950419Z"
  }
  ```
- **Chaos Active (`503 Service Unavailable`)**:
  ```json
  {"detail": "Chaos probe injected failure: 503 Service Unavailable"}
  ```

---

### 10. `POST /cache/clear`
Purges all entries from both the Redis vector index and the in-memory fallback store.
If Redis is down, the in-memory store is emptied at once and the Redis purge stays pending until the next successful reconnect, before Redis serves any lookup. The response is the same either way.

#### Headers
| Header | Type | Description |
| :--- | :--- | :--- |
| `X-Admin-Key` | `string` | **Required.** Must equal the server's `ADMIN_API_KEY` (`401` if missing or wrong; `403` if the server has no key configured). |

#### Response (`200 OK`)
```json
{
  "status": "ok",
  "message": "Semantic cache successfully cleared"
}
```

#### Example cURL
```bash
curl -X POST http://127.0.0.1:8000/cache/clear \
  -H "X-Admin-Key: secret-admin-key"
```

