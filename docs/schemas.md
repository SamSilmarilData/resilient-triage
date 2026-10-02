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
| `degradation_status` | `DegradationStatus` | Agent operational mode | `HEALTHY`, `PARTIALLY_DEGRADED`, `SEVERELY_DEGRADED`, `UNKNOWN` |
| `circuit_breakers_tripped` | `list[str]` | Names of tripped breakers | E.g. `["statuspage", "chaos"]` |
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

### `HealthResponse`
- `status`: Service status (`"healthy"` or `"degraded"`).
- `version`: API release version string (`"1.0.0"`).
- `redis_connected`: True if Redis is reachable and active.
- `cache_driver`: Active vector driver (`"redisearch"`, `"redis_hash_fallback"`, or `"in_memory"`).
- `circuits`: Dictionary mapping breaker names to states (`{"statuspage": "closed", "chaos": "closed"}`).
- `active_model`: Identifier of the active LLM inference engine.

### `ResilienceStatusResponse`
- `circuits`: Dictionary mapping breaker names to deep diagnostic `CircuitSnapshot` models (state, failure counts, last state change, last failure reason).
- `chaos_active`: Boolean flag indicating if runtime chaos simulation is active.
- `cache_driver`: Name of the active vector search driver.
- `cache_entries_count`: Count of documents cached in memory.
- `active_model`: Active LLM engine name.
