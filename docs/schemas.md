# Data Contracts & Schemas

All data contracts in `resilient-triage` are strictly typed using Pydantic v2 with `extra="forbid"` on incident models to ensure deterministic output generation.

## Incident Models (`resilient_triage.schemas.incident`)

### `IncidentTriageReport`
The primary validated output produced by the triage agent.

| Field | Type | Description | Invariants |
| :--- | :--- | :--- | :--- |
| `incident_id` | `str` | UUID identifier for report | Default generated |
| `timestamp` | `datetime` | UTC timestamp of report | Default `now(timezone.utc)` |
| `summary` | `str` | High-level overview of incident | Minimum length 5 characters |
| `severity` | `SeverityLevel` | Severity classification | One of `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `UNKNOWN` |
| `root_cause_analysis` | `str` | Suspected root cause | Minimum length 5 characters |
| `affected_services` | `list[ServiceImpact]` | List of impacted components | Non-hallucinated list |
| `telemetry_signals` | `list[TelemetrySignal]`| Signals gathered from telemetry | Recorded observations |
| `recommended_actions` | `list[ActionRecommendation]` | Remediation steps | Priority tagged |
| `degradation_status` | `DegradationStatus` | Agent operational mode | `HEALTHY`, `PARTIALLY_DEGRADED`, `SEVERELY_DEGRADED`, `UNKNOWN`. Set by report reconciliation (the LLM's value is discarded); `UNKNOWN` is never produced |
| `circuit_breakers_tripped` | `list[str]` | Names of tripped breakers | E.g. `["statuspage", "chaos"]`. Set by report reconciliation from the targeted breakers open at collection (the LLM's value is discarded) |
| `confidence_score` | `float` | Model confidence score | Constrained between `0.0` and `1.0` |

### Enums
- **`SeverityLevel`**: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `UNKNOWN`
- **`DegradationStatus`**: `HEALTHY`, `PARTIALLY_DEGRADED`, `SEVERELY_DEGRADED`, `UNKNOWN`
- **`PriorityLevel`**: `P0` (immediate action required), `P1`, `P2`, `P3`

---

## Telemetry Models (`resilient_triage.schemas.telemetry`)

### `StatuspageSummary`
Parsed payload from Atlassian Statuspage `/api/v2/summary.json`:
- `page_name: str`
- `indicator: str` (`none`, `minor`, `major`, `critical`)
- `components: list[StatuspageComponent]`
- `incidents: list[StatuspageIncident]`

### `ChaosConfig`
Runtime parameters for the configurable `/chaos` endpoint:
- `active: bool`: Master toggle for chaos injection.
- `inject_503: bool`: If true, immediately raises HTTP 503.
- `failure_rate: float`: Probability fraction between `0.0` and `1.0`.
- `latency_ms: int`: Milliseconds of artificial latency to introduce.

---

## LangGraph State (`resilient_triage.schemas.state`)

### `TriageState`
Typed dictionary carrying execution state across graph nodes:
- `query: str`: Raw incident query.
- `messages: Annotated[list[Any], add_messages]`: Conversation and repair messages.
- `statuspage_telemetry: dict | None`: Telemetry payload from external statuspage.
- `chaos_telemetry: dict | None`: Telemetry payload from chaos simulator.
- `circuit_breaker_status: dict[str, str]`: Circuit breaker states (`CLOSED`, `OPEN`, `HALF_OPEN`).
- `is_degraded: bool`: Flag indicating degraded execution.
- `candidate_report_raw: str | None`: Unparsed raw output from LLM.
- `repair_attempts: int`: Number of self-repair retries attempted (max 2).
- `validation_errors: list[str]`: Tracebacks from Pydantic `ValidationError`.
- `final_report: IncidentTriageReport | None`: Final validated report.

---

## API Contracts (`resilient_triage.schemas.api`)

### `TriageRequest`
- `query: str`: Incident symptoms or inquiry (minimum length 3).
- `service_filter: str | None`: Optional component scope.
- `force_refresh: bool`: Set `true` to bypass semantic cache.

### `TriageResponse`
- `report: IncidentTriageReport`: The triage report.
- `cached: bool`: Whether served from semantic cache (`X-Cache: HIT`).
- `cache_similarity: float | None`: Cosine similarity score if matched in cache.
- `execution_time_ms: float`: Request turnaround time in milliseconds.

### `CircuitSnapshot`
Snapshot representation of an individual circuit breaker's operational state:
- `name: str`: Unique breaker identifier (e.g., `"statuspage"`, `"chaos"`).
- `state: str`: Current state (`"closed"`, `"open"`, or `"half-open"`).
- `fail_counter: int`: Consecutive failure count.
- `fail_max: int`: Failure threshold required to trip the breaker.
- `is_open: bool`: Boolean indicating if the breaker is open.
- `last_failure_time: datetime | None`: Timestamp of the most recent failure.
- `last_state_change: datetime | None`: Timestamp of the most recent state transition.
- `last_failure_reason: str | None`: Error message or reason for the latest failure.

### `LLMQuotaSnapshot`
Quota, rate limit and failover telemetry from the LLM gateway (`LLMGateway.snapshot()`). The budget fields and `last_status` describe the primary model only; the counters cover both models.

- `provider: str`: Provider name (`"groq"`).
- `active_model: str`: The model that served the last completion (the primary `"qwen/qwen3.8-27b"` until a fallback answers).
- `fallback_model: str | None`: The fallback model, `GROQ_FALLBACK_MODEL` (`"llama-3.1-8b-instant"`).
- `remaining_requests: int | None`: Requests remaining in current rate-limit window.
- `limit_requests: int | None`: Total request limit for current window.
- `remaining_tokens: int | None`: Tokens remaining in current rate-limit window.
- `limit_tokens: int | None`: Total token limit for current window.
- `reset_requests: str | None`: Duration until request quota window resets (e.g., `"12.4s"`).
- `reset_tokens: str | None`: Duration until token quota window resets (e.g., `"8.1s"`).
- `total_calls: int`: Model calls made since the app started or the last reset. A `complete()` that fails over counts each call.
- `rate_limit_hits: int`: Total HTTP 429 rate limit events encountered.
- `failover_events: int`: Completions answered by the fallback model, after a primary failure or while the primary cooled down.
- `last_status: int | None`: HTTP status code of the primary model's most recent response.
- `last_error: str | None`: Most recent error message or rate limit description.
- `last_updated: datetime | None`: Timestamp of the most recent telemetry update.

### `HealthResponse`
- `status`: Service status (`"healthy"` or `"degraded"`).
- `version`: API release version string (`"1.3.0"`).
- `redis_connected`: True if Redis is reachable and active.
- `cache_driver`: Active vector driver (`"redisearch"`, `"redis_hash_fallback"`, or `"in_memory"`).
- `circuits`: Dictionary mapping breaker names to states (`{"statuspage": "closed", "chaos": "closed"}`).
- `active_model`: Identifier of the active LLM inference engine.
- `llm_quota`: Optional `LLMQuotaSnapshot` of active provider rate limits and failover stats.

### `ResilienceStatusResponse`
- `circuits`: Dictionary mapping breaker names to deep diagnostic `CircuitSnapshot` models.
- `chaos_active`: Boolean flag indicating if runtime chaos simulation is active.
- `cache_driver`: Name of the active vector search driver.
- `cache_entries_count`: Count of documents cached in memory.
- `active_model`: Active LLM engine name.
- `llm_quota`: Optional `LLMQuotaSnapshot` detailing rate limits, failover counts, and remaining quotas.

