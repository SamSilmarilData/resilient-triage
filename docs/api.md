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
| `x-thread-id` | LangGraph persistence checkpoint thread identifier — *present on `/triage`* | `e1f8b4a2-...` |

### Security & Authentication Headers
- `X-Admin-Key`: Required for mutating endpoints (`/resilience/reset`, `/cache/clear`, `/chaos/inject`) if `ADMIN_API_KEY` is configured in the environment. Unauthenticated requests receive `401 Unauthorized`.

---

## API Endpoints Summary

| Method | Endpoint | Description | Auth |
| :--- | :--- | :--- | :---: |
| `POST` | [`/triage`](#1-post-triage) | Run autonomous incident triage analysis | Public |
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
> **Concurrency Load Shedding**: Execution is bounded by an async semaphore (max 10 concurrent LangGraph workflows). If saturated with a wait exceeding 5.0 seconds, the gateway sheds load with `HTTP 503 Service Unavailable`.

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
  "execution_time_ms": 1296.74
}
```

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

### 2. `GET /health`
Liveness and readiness health probe for Kubernetes/load balancers, returning service availability, Redis connection driver, and circuit breaker states.

#### Response (`200 OK`)
```json
{
  "status": "healthy",
  "version": "1.3.0",
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
    "fallback_model": "openai/gpt-oss-20b",
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
Retrieves live rate limit budgets, remaining token allowances, and failover metrics captured from Groq API response headers.

#### Response (`200 OK`)
```json
{
  "provider": "groq",
  "active_model": "qwen/qwen3.8-27b",
  "fallback_model": "openai/gpt-oss-20b",
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
Administrative reset restoring all circuit breakers to `CLOSED`, clearing chaos injection rules, and resetting LLM quota counters.

#### Headers
| Header | Type | Description |
| :--- | :--- | :--- |
| `X-Admin-Key` | `string` | *(Optional)* Required if `ADMIN_API_KEY` is configured in gateway environment. |

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
| `X-Admin-Key` | `string` | *(Optional)* Required if `ADMIN_API_KEY` is configured in gateway environment. |

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
| `latency_ms` | `integer` | Artificial lag to introduce in milliseconds. |

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

#### Headers
| Header | Type | Description |
| :--- | :--- | :--- |
| `X-Admin-Key` | `string` | *(Optional)* Required if `ADMIN_API_KEY` is configured in gateway environment. |

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

