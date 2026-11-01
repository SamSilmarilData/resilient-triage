# Architecture & System Design

This document details the architectural principles, state machine topology, and resilience patterns implemented in `resilient-triage`.

---

## 1. Design Philosophy

During high-severity incidents, the infrastructure supporting monitoring and alerting often experiences failures, timeouts, and throttling. Standard LLM agents that make synchronous, unguarded HTTP calls or rely on brittle output parsing fail catastrophically in these conditions.

`resilient-triage` is engineered on four core architectural pillars:
1. **Never stall on dead dependencies**: Use circuit breakers with fail-fast degraded fallbacks.
2. **Absorb transient turbulence**: Wrap external telemetry calls with exponential backoff and randomized jitter; model calls get a single jittered retry and a fallback model from the LLM gateway.
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

### App runtime
Each app instance owns one `AppRuntime` (`runtime.py`), built by `build_runtime(settings)` and started and closed by the FastAPI lifespan. It holds every stateful module: the semantic cache, the circuit breaker registry, the chaos manager, the LLM gateway (`AppRuntime.llm`, which owns its model adapter and, for Groq, the Groq HTTP client), and the Statuspage HTTP client (the Statuspage summary cache lives inside its Statuspage source). Graph nodes see these only through `TriageDeps` (`graph/deps.py`; `TriageDeps.llm` is the same gateway), passed as LangGraph's run context (`triage_graph.ainvoke(..., context=runtime.deps)`). The graph is compiled without a checkpointer (ADR-007): each run is self-contained, its state lives only while `ainvoke` runs, and no state carries from one request to the next. There are no module-level singletons; only `settings`, the FastEmbed embedder and its LRU, and the compiled `triage_graph` are process-wide. Tests build a fresh runtime per test.

### Request path: the Triage gateway
`POST /triage` is a thin adapter over `TriageGateway` (`triage/gateway.py`), an `AppRuntime` member (`runtime.triage_gateway`). `triage(query, scope, force_refresh=..., trace=...)` returns a *triage outcome*: `CacheHit(response)`, `Analyzed(response)` or `Shed()`, and the route matches on it exhaustively (`X-Cache: HIT` / `MISS`, or 503). The gateway builds the `TriageResponse` itself (report, cache fields, `execution_time_ms` and the trace summary), so `POST /triage` and the `trace_finished` event carry the same response. The gateway:
1. Looks up the semantic cache unless `force_refresh` is set; a hit is `CacheHit`.
2. Free play only: asks the free-play run budget (`RunBudget`, `triage/budget.py`, `free_play_runs_per_minute` graph runs per sliding 60 s, per process) to admit the run; a refused run is shed at once (`Shed(retry_after_s=...)`, HTTP 503 with `Retry-After`, `run_slot(reason=budget)`). Then waits up to `triage_queue_timeout_s` (5s) for one of `triage_max_concurrent_runs` (10) run slots; if none frees in time the request is shed (`Shed()`, HTTP 503, `run_slot(reason=capacity)`). The slots are a `RunSlotPool` (`triage/slots.py`: capacity and queue timeout); the runtime's gateway builds its own from those settings, a scenario run's gateway is handed one (below), and `run_slot.capacity` reports the pool's capacity.
3. Runs the graph through its `GraphRunner` (`AppRuntime.run_graph(run_input, trace=)`, which passes `context=dataclasses.replace(runtime.deps, trace=trace)`), releasing the slot in a `finally`, under a run deadline of `triage_run_deadline_s` (45s; past it the run is cancelled and answered with an uncached synthesis fallback, see [resilience](resilience.md#run-deadline-a-stalled-run-answers-with-a-fallback)). If the graph returns no `final_report`, the gateway builds a synthesis fallback with `build_fallback_report` and reconciles it.
4. Stores the report under the request's service scope, unless it is a synthesis fallback (never cached, see [resilience](resilience.md#degradation-status-report-reconciliation-in-finalize)), and returns `Analyzed`. A store that raises is logged and the report is still answered (`cache_store` outcome `failed`).

The gateway is the only code on the request path that writes to the semantic cache: graph nodes get a read-only `CacheReader` (`TriageDeps.history`) and `finalize` only reconciles. It also counts outcomes (`TriageGateway.metrics`: `hit`, `miss`, `saturated`), which `/metrics` exports as `resilient_triage_requests_total{cache=...}`.

### Scenario runs: private collaborators per run
A scenario run replays a catalogued scenario on collaborators no other run can see (ADR-009). `scenarios/model.py` holds the definition, a frozen `Scenario`: `id`, `query`, `scope`, `script` (the model's replies in order, each a `str`, a provider failure, or a `ScriptedReply(reply, delay_s)` whose delay is a real `asyncio.sleep`), `statuspage` (a fixed `StatuspageSummary`, or `None` for a Statuspage that always fails), `cluster_sentinel` (a `ChaosConfig` for the private chaos manager), `setup`, `overrides`, the page copy (`chapter`, `title`, `caption`, `setup_notes`) and `expected_outcome`, an `ExpectedOutcome` (triage outcome, degradation status, node path, `(router source, route key)` pairs, model calls, run-time source reads, tripped breakers, approvals raised by reconcile, confidence, cache store, run deadline). A definition that could not repeat exactly (a random `failure_rate`) or that trips a source which does not fail is rejected when built. `Scenario.public_view()` returns a `ScenarioView` (`schemas/api.py`: id, chapter, title, caption, setup notes) for the page; it never carries the script, query or expected outcome.

The catalogue (`scenarios/catalogue.py`) holds the 13 scenarios and all their copy: `CHAPTERS` (`Chapter(id, title)`, in display order: `fast-paths`, `model-misbehaves`, `dependencies-fail`, `finale`), `SCENARIOS` (in display order, grouped by chapter) and `get_scenario(id)`. Scripted replies arrive after 0.9 s and failures after 0.18 s. Self-repair exhausted takes two invalid replies, one self-repair round. Redis down needs no flag: every scenario cache already runs on its in-memory standby, so `everything-down` only names it in its setup notes. `tests/test_scenario_catalogue.py` runs every scenario through the real factory against its expected outcome, and checks that the catalogue's routes cover every router branch of the graph.

`ScenarioFactory(settings=, slots=, embedder=)` (`scenarios/factory.py`) is built once, by `AppRuntime` (`runtime.scenarios`), with the shared scenario slot pool (`runtime.scenario_slots`, `scenario_max_concurrent_runs` slots) and the runtime cache's embedder; `build(scenario)` returns a `ScenarioRun` with a fresh scripted `LLMGateway`, a `TelemetryProbe` over `CannedStatuspageSource` or `FailingStatuspageSource` (`telemetry/canned.py`, both `id = "statuspage"`, no HTTP) and a Cluster Sentinel on a private `ChaosManager`, a fresh `CircuitBreakerRegistry`, an in-memory `SemanticCacheManager` and a `TriageGateway` whose graph runner passes the run's own `TriageDeps`. It performs no I/O and never imports or touches `AppRuntime`, so the runtime's breakers, cache, chaos, model cooldowns and `/metrics` counts are unchanged by any scenario run; the run's own outcome counts are dropped with its gateway. The private cache's remote store is an `UnreachableRemoteStore` (`cache/unreachable_store.py`) whose connect fails, so the cache never reaches the shared Redis and runs on its in-memory standby through the real failover path, which is also how Redis being down looks in a scenario.

`ScenarioRun.triage(trace=)` and `ScenarioRun.stream()` (the live events, for a streamed route) hand the gateway the scenario's id and its setup (`TriageGateway.triage(..., scenario_id=, setup=)`). The gateway runs the setup on the request's recorder, in the stream's run task, before it records `trace_started`, so every setup event comes before it, stamped `t_ms` 0, and setup time never counts as run time. A setup that raises is logged and answered like any unexpected exception: an uncached `SEVERELY_DEGRADED` synthesis fallback (`trace_started`, `reconciled`, `trace_finished`), never a 500. The setup steps:
- **Seeds** (`Setup.seeds`): each `CacheSeed` is stored through the private cache's real `store()`, then one `scenario_setup(step=seed_cache, count=)`.
- **Tripped breakers** (`Setup.tripped_sources`): real reads of the failing source, each a `telemetry_read` with `phase: "setup"`, until its private breaker opens, which is exactly `circuit_breaker_fail_max` reads; then `scenario_setup(step=fail_source, count=, source=)`.
- **Held slots** (`Overrides.held_slots`): the run gets a private `RunSlotPool` instead of the shared one, the setup takes every slot (`scenario_setup(step=hold_run_slots)`), and the real queue wait and timeout shed the request. The held slots are given back when the run ends, however it ends.
- **Run deadline** (`Overrides.run_deadline_s`): the run's gateway gets a copy of the settings with that deadline; the shared settings are untouched. The setup shows it as `scenario_setup(step=set_run_deadline, count=1, deadline_s=)`.

`GET /scenarios` serves `public_catalogue()` (a `ScenarioCatalogue`: chapters in order, each with its scenarios' `ScenarioView`s). `POST /scenarios/{scenario_id}/stream` looks the id up (404 before any stream if unknown) in a request-scoped yield dependency, `open_scenario_stream`, which enters `runtime.scenarios.build(scenario).stream()` through the same `_client_stream` helper that `open_trace_stream` uses for the free-play gateway's stream.

### Execution trace
An execution trace records what happened while one triage request was handled (ADR-008). `src/resilient_triage/trace/` holds its parts. The Triage gateway records the request path and hands the same recorder to the graph run, which records its own events.

- **Trace events** (`trace/events.py`): frozen Pydantic models in one discriminated union on `kind` (`parse_trace_event()` validates one; an unknown `kind` is rejected). Every event has `seq` (0, 1, …) and `t_ms` (monotonic ms since `trace_started`; scenario setup, recorded before it, is stamped 0 and ordered by `seq`, so setup time never counts as run time). Fields are ids, Literals, enums, counts and timings only; every string field is a Literal or matches the `Identifier` pattern (`trace/fields.py`), so no query text, prompt, model output, signal snippet or Pydantic `input` fits. The report travels once, in `trace_finished`. `ModelCall.from_result`, `ValidationFailed.from_error` and `Reconciled.between` project domain objects to events and drop their text.
- **Recorders** (`trace/recorder.py`): the `TraceRecorder` protocol has a synchronous `record(event)` that stamps `seq`/`t_ms` and never raises (exceptions are logged and swallowed). `NullTrace` drops events; `ListTrace` keeps them and feeds an optional sink; `StreamingTrace` is a `ListTrace` whose sink is an unbounded, lossless `asyncio.Queue`, read by `follow()` until `close()`. One recorder per request, never a module-level singleton. `record_event(trace, build)` is how instrumented code records: it builds the event and records it inside one `try`, so neither a bad event nor a recorder that breaks the never-raises contract can change a run's result. `trace/recorder.py` imports `trace.events` and `trace.summary` only for type checking (or lazily), so `llm.gateway`, which `schemas` imports, can import it without a cycle.
- **Recording a graph run**: the recorder rides on the run's context, `dataclasses.replace(deps, trace=recorder)`. `TriageDeps` is shared by every run and never mutated; its `trace` defaults to `NullTrace`, so a run without a recorder (and `/chaos/*`, which calls the probe directly) records nothing.
  - **Node timing** (`graph/builder.py`): every node is registered through `_traced(name, node, routes)`, which records `node_started`, then `node_finished` with `duration_ms` and `status` (`ok`; `error` if the node raised; `cancelled` on `CancelledError`, e.g. the run deadline) and re-raises unchanged. It also adds `previous_node` to the node's state update.
  - **Routes are derived, routers stay pure**: before a node starts, the wrapper looks up the (`previous_node`, node) pair in `route_labels(compiled_graph)`, built from `get_graph()`'s conditional edge labels; a pair found there is recorded as `route_decided(source, target, route)`. Unconditional edges record nothing. `route_labels` raises if any pair can be reached by two router keys. It reads the builder's path maps for that check, because `get_graph()` draws one edge per pair and silently keeps only the first key.
  - **Model calls**: `generate_report` passes `runtime.context.trace` to `LLMGateway.complete(..., trace=)`, which records one `model_call` per adapter attempt (`ModelCall.from_result`: model, `primary`/`fallback` role, outcome kind, 429 scope and retry-after). An adapter that raises is one `unavailable` call. A call skipped because both models cool down records none.
  - **Telemetry reads**: `collect_telemetry` passes it to `TelemetryProbe.collect(..., trace=, phase=)`, which records one `telemetry_read` per source read: outcome (`read`/`tripped`/`unavailable`), `attempts` (calls to the source's `read`, counted inside the retry policy, so a breaker refusal is 0), breaker state before and after, signals added. `phase="setup"` marks a scenario's pre-run read.
  - **Validation and reconciliation**: `validate_report` records `validation_failed` (`ValidationFailed.from_error`: declared field names and Pydantic error types only) when a candidate fails. The wrapper puts the output report's `confidence` (what `route_after_validation` reads) on `validate_report`'s `node_finished`, and records `reconciled` (`Reconciled.between`) from `finalize_report`'s input and output reports, so `finalize` stays a pure function of state.
- **Recording the request path** (`triage/gateway.py`): `TriageGateway.triage(..., trace=)` takes the request's recorder (`NullTrace` when omitted) and records `trace_started` (`scenario_id=None` for `POST /triage`), `cache_lookup` (skipped under `force_refresh`), `run_slot`, the graph run's events, `cache_store` (`stored` / `skipped_fallback` / `failed`) and, last, `trace_finished`. A cache hit is exactly three events; a shed request ends `run_slot(acquired=false)`, `trace_finished(outcome=shed, response=None)`. The synthesis fallbacks the gateway builds outside the graph (run deadline, an unexpected `Exception`, no final report) record `reconciled` there with `Reconciled.between`; a deadline records `deadline_exceeded` first (after the cancelled nodes' `node_finished`), so the order is `deadline_exceeded`, `reconciled`, `cache_store(skipped_fallback)`, `trace_finished`. A `CancelledError` (a client disconnect) propagates with the slot released, records no `trace_finished`, and counts once in the gateway's `abandoned` metric (not a triage outcome). `POST /triage` passes a `ListTrace` per request.
- **Streaming the request path** (`TriageGateway.stream()`, `POST /triage/stream`): an async context manager that runs `triage()` on a `StreamingTrace` as its own task and yields the live event iterator; leaving it, normally or by cancellation, cancels that task and waits for it. If `triage()` raises an `Exception`, the task still finishes the trace with a reconciled synthesis fallback in `trace_finished` (counted as a miss, not cached). The route enters it in a request-scoped yield dependency, not in its own generator: FastAPI's SSE producer parks the generator at a `yield`, so on a disconnect its `finally` would only run when garbage-collected, leaking the run and its slot. Each event goes out as `ServerSentEvent(event=kind, data=event)`. A run task cancelled before its first step never enters `triage()`, so `stream()` counts that abandonment itself (once either way); the dependency also catches the `BrokenResourceError` FastAPI's SSE keepalive raises when a client disconnects with an event or ping unsent (see [resilience](resilience.md#streamed-requests-one-trace_finished-and-abandoned-runs)).
- **Trace summary** (`trace/summary.py`): `summarize(events)` is a pure function to a structured `TraceSummary` (scenario id, outcome, node path with durations, route keys, model calls, failovers, run-time source reads, repair and refine rounds, reconciliation changes, degradation, deadline, timings). It works without `trace_finished`, because the summary travels inside that event's response: the gateway sets `TriageResponse.trace` to `summarize()` of the events recorded before `trace_finished` (only for a recorder that keeps its events, a `ListTrace`; otherwise `null`). Its `degradation` is therefore `null` on a cache hit, where no reconcile ran in the request; the report carries its own status. Timings are rounded to microseconds. `trace.summary` imports `trace.events` only for type checking, so `schemas.api` can import `TraceSummary`.

Each kind's fields are listed in the [event table](api.md#execution-trace-events).

### Frontend assets
The page is build-free (ADR-010): native ES modules, one stylesheet and self-hosted fonts, served same-origin from `src/resilient_triage/api/static/`, which is package data (`static/**`).

- **Serving** (`api/static_assets.py`): `create_app` mounts `StaticAssets` at `/static`, one instance per app. It is Starlette's `StaticFiles` with a fixed media-type table, so `.mjs` is `text/javascript` and `.woff2` is `font/woff2` whatever the host's MIME database holds (the global `mimetypes` registry is not touched), and a `Cache-Control` on every file: `immutable` for a year under `vendor/`, whose paths carry versions, and `no-cache` (revalidate) for everything else. Headers per path are in [api.md](api.md#11-get-staticpath).
- **Vendored packages** (`static/vendor/`): Preact 10.29.8 (+ hooks), htm 3.1.1 (+ `htm/preact`), `@preact/signals` 2.11.4, `@preact/signals-core` 1.14.5, and the Inter and JetBrains Mono variable woff2 Latin subsets (Fontsource 5.3.0), each at `<npm name>@<version>/<path in the package>` with its LICENSE. The JavaScript files are each package's `.mjs` export; the manifest gives each its bare import `specifier` (`preact`, `preact/hooks`, `htm`, `htm/preact`, `@preact/signals`, `@preact/signals-core`), from which the page's import map is written. `htm/preact/standalone` is never vendored.
- **The page shell** (`static/index.html`, `app.mjs`, `styles.css`): `GET /` serves `index.html` (`Cache-Control: no-cache`). Its prose (the hook, the free-play intro, "how it's built"), landmarks and a `<noscript>` note are static HTML, so they render before any script runs and survive a failed `/scenarios` (Deferred UI questions, #101). One import map gives each bare name one same-origin URL, written from the manifest's `specifier`s. `app.mjs` (Preact, htm and Signals, `// @ts-check`) renders the three mount points (the live-trace column, the chapters, free play) with empty states, removes their `aria-busy`, then sets `data-shell="ready"` on `<html>`, the DOM state browser tests wait on. `styles.css` is one hand-written stylesheet: an `@layer` order, OKLCH tokens on `:root`, a dark theme by `prefers-color-scheme` overridden by `<html data-theme="light|dark">`, native nesting, `prefers-reduced-motion` honoured, and the vendored fonts (`Inter Variable`, `JetBrains Mono Variable`) with `font-display: swap`. The vendored Latin subsets have no arrow glyphs, so copy avoids them. The live-trace column is pinned beside the chapters from 60rem and inline above them below that.
- **Graph layout** (`static/graph-layout.json`): one hand-placed top-to-bottom layout of `triage_graph.get_graph()` used at every width: node centres and widths in SVG units, `__start__`/`__end__` as terminals, each edge an orthogonal polyline, and each conditional edge keyed by its router label (`route`, as in `route_decided`, ADR-008) with a label position. It is 360 units wide, so at 375 px its 12-unit node labels still render at about 11 px. `tests/test_graph_layout.py` compares it with `get_graph()` both ways (a missing or stale node or edge fails) and checks the placement: boxes fit their names and don't overlap, edges run border to border through no other node, labels clear nodes and each other.
- **Static checks** (`tests/test_static_assets.py`): the import map equals the manifest's specifiers; every specifier in our and the vendored `.mjs` (minified `from"x"`, `export … from`, side-effect and dynamic `import()`) resolves to a shipped file, bare names only through the import map and vendored modules only by bare name, so there is one Preact, and `htm/preact/standalone` is never named; nothing the HTML or CSS fetches is on another origin (an `<a href>` is navigation, not a request); and the theme tokens reach WCAG AA contrast (4.5:1 text, 3:1 focus ring) on every surface in both themes.
- **Browser tier** (`tests/browser/`, ADR-010): Playwright drives the page in headless Chromium. Tests are marked `browser` and deselected by default; `./scripts/check.sh --browser` runs only them, and CI's `browser` job runs them on every push. The harness serves `create_app` with uvicorn on a background thread at `127.0.0.1:<free port>`, over a runtime of its own (`ScriptedAdapter` model, canned Statuspage, deterministic embedder), so free play never reaches Groq and nothing downloads a model. Every browser context routes `**/*` through a guard that aborts any request off the live app's own origin (scheme, host and port) and fails the test. Tests wait on DOM state (`html[data-shell="ready"]`, network idle, `document.fonts.ready`), never a timer. `tests/browser/test_shell.py` checks the shell loads with no console errors and no failed (or 4xx/5xx) or off-origin requests, has no serious or critical axe-core violation in light and dark (by the system preference and by `data-theme`), and never scrolls sideways at 375 px. axe-core 4.14.0 is test-only: it is vendored under `tests/vendor/` with its licenses, pinned by sha256 in `tests/vendor/manifest.json` (checked by `vendor.py verify` in the regular run), and injected with `page.add_script_tag`, never served by the app.
- **Manifest and `scripts/vendor.py`**: `static/vendor/manifest.json` pins each package's npm tarball URL and integrity and each file's sha256. `vendor.py verify` (stdlib only, offline) checks every file against it, that each package lists its license file, and that no unlisted file sits under `vendor/`; `tests/test_static_assets.py` runs the same check. `vendor.py update` is the only step that touches the network and is run by hand (see [deployment](deployment.md#vendored-frontend-files)).

---

## 3. Deep-Dive Components

### 1. Redis 8 RediSearch Semantic Caching
- **Purpose**: Prevent redundant LLM evaluations and downstream API calls during an ongoing incident when multiple engineers ask similar questions.
- **Mechanism**:
  - Ingress queries are embedded into a dense 384-dimensional vector in ~3.5ms via FastEmbed ONNX runtime (`BAAI/bge-small-en-v1.5`).
  - RediSearch performs KNN similarity search using an HNSW Float32 Cosine distance index (`triage_vector_idx`).
  - If cosine similarity $\ge 0.90$, the cached `IncidentTriageReport` is immediately returned in **`7.71ms`** with header `X-Cache: HIT`.
  - Cache misses proceed to the LangGraph state machine, and the Triage gateway (the only request-path cache writer) stores the final report (unless it is a synthesis fallback) under `triage:doc:*` with its service scope and a TTL (300s healthy, 60s degraded).
  - **Service scope is exact**: a lookup reuses only an entry stored under exactly the same normalized scope (`normalize_scope()`), and "no scope" is a scope of its own, not a wildcard. RediSearch fetches the `SEARCH_KNN = 5` nearest neighbours and takes the best one in the exact scope; if none of the 5 is, the lookup is a MISS.
- **Structure** (`cache/`):
  - `vector_store.py` — the `VectorStore` seam (`search`, `put`, `delete`, `clear`, `count`), its `RemoteVectorStore` extension (`connect`, `ping`, `aclose`, `supports_search`, `entries`), the `VectorEntry` / `VectorHit` records, and the two failure kinds `StoreUnavailable` and `StoreQueryError`.
  - `redis_store.py` — `RedisVectorStore`: the client, search-module detection, HNSW index creation, the KNN query, hash writes, `SCAN` + batched `DEL` clear, and the `entries()` scan used for hydration.
  - `in_memory_store.py` — `InMemoryVectorStore`: NumPy cosine search over L2-normalized vectors, TTL purge, oldest-first eviction at `max_capacity`, and a dimension guard that rejects wrong-size vectors.
  - `semantic_cache.py` — `SemanticCacheManager`: scope normalization, embedding, TTL choice, write-through to the in-memory standby (and Redis while it is up), read routing, and the single failover path `_mark_down`.
- **Zero-Downtime Resilience**:
  - `lookup`, `store` and `clear` never raise. A Redis drop, a Redis query error, an embedding failure or a cached report that fails validation becomes a MISS or a logged no-op, so the request runs the graph instead of returning a 500.
  - On a connection-class failure (`StoreUnavailable`), `_mark_down` closes the Redis client and the manager serves from the in-memory standby. A `StoreQueryError` is logged and Redis stays connected.
  - There is no background probe. While Redis is down, the next `lookup` or `store`, or a status route (`/health`, `/resilience/status`, `/metrics` call `check_connection()`), tries to reconnect at most once per 5-second cooldown, with no server restart.
  - See [resilience.md §4](resilience.md#4-zero-downtime-dual-engine-vector-caching) for the failover rules and [ADR-0002](decisions/0002-dual-engine-semantic-caching.md) for the decision.

---

### 2. LLM Gateway (Groq LPUs)
- **Model**: `qwen/qwen3.8-27b` on Groq (`GROQ_MODEL`), failing over to `llama-3.1-8b-instant` (`GROQ_FALLBACK_MODEL`).
- **Latency**: **~1.1s** generation turnaround (166ms TTFT).
- **Protocol**: Direct async HTTP with strict `response_format={"type": "json_object"}`, one request per call, timing out after `LLM_REQUEST_TIMEOUT_S` (default 20s).
- **One seam**: every model call goes through `LLMGateway.complete(messages)` in `src/resilient_triage/llm/` (ADR-006). It returns a `Completion` (text plus the model that answered) or a **provider failure**, and never raises anything except `CancelledError`. The failure kinds are:

  | Kind | Cause |
  | :--- | :--- |
  | `RateLimited(retry_after_s, scope)` | HTTP 429. `scope` is `minute`, `day` or `unknown`. |
  | `Unavailable` | 5xx, timeout or connection error. |
  | `Rejected` | A 4xx other than 429. |
  | `MalformedResponse` | A 2xx without `choices[0].message.content`. |

  A completion that is not valid report JSON is not a provider failure; validation and self-repair handle it (ADR-004).
- **Adapters**: the gateway owns model failover and per-model cooldowns ([resilience guide](resilience.md#6-llm-gateway-failover-and-rate-limit-cooldowns)). An adapter makes exactly one call to one model:
  - `GroqAdapter` (`llm/groq.py`) owns the pooled `httpx.AsyncClient`, maps each response to a result, and parses the rate-limit headers.
  - `ScriptedAdapter` (`llm/scripted.py`) returns scripted results in order, then a valid default report. It is both the test fake and the zero-config simulation.
- **Provider selection**: `build_llm_gateway(settings, adapter=None)` (`llm/__init__.py`) is the only place a provider is chosen. With `GROQ_API_KEY` set it uses `GroqAdapter` and the `/health` label `Groq LPU (<GROQ_MODEL>)`; without it, `ScriptedAdapter` and `Zero-Config SRE Simulation`. No other key selects a provider. Each adapter supplies its own label (`ModelAdapter.label(primary)`) and rate-limit headers (`quota_headers(model)`), so the gateway never checks an adapter's type. `build_runtime(settings, llm_adapter=...)` passes an adapter through, which is how tests script the model.
- **Graph contract**: `generate_report` calls `runtime.context.llm.complete(messages)`. It records the typed failure in `provider_failure` (`None` after a completion), and `route_after_generation` sends a failure past `self_repair`, since re-prompting does nothing for an outage: to `finalize` with the pre-refine report during a refine round, else to `fallback_report`. `fallback_report` picks its root-cause wording by the failure kind. The kinds live in `llm/failures.py`, which has no project imports, so `TriageState` can import it without a cycle (importing it still runs `llm/__init__`).

---

### 3. Resilience Layer: Tenacity Retries
- **Purpose**: Absorb transient networking hiccups, intermittent 503s, and HTTP 429 rate limits from telemetry sources. Model calls are not wrapped: the LLM gateway applies its own failover policy (section 2).
- **Parameters**:
  - Retry on `httpx.TimeoutException`, `httpx.NetworkError`, and specific HTTP status codes (429, 502, 503, 504).
  - Exponential backoff with randomized jitter (`wait_random_exponential`) to prevent thundering herd problems against upstream services.

---

### 4. Resilience Layer: Pybreaker Circuit Breakers
- **Purpose**: Prevent cascades of hanging requests when an upstream provider (such as an external Statuspage or an internal telemetry endpoint) is completely offline.
- **State Machine**:
  - `CLOSED`: Normal operation. Calls pass through.
  - `OPEN`: If 3 consecutive failures occur (`fail_max=3`), the breaker trips `OPEN`. All subsequent calls fail immediately with `BreakerOpen` without placing network load.
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

- **`analyze_query`**: Asks the guarded telemetry probe which **Targeted sources** to read (`runtime.context.telemetry.select_targets(query, service_filter)`) and writes them, sorted, as `targeted_sources`. Each Telemetry source carries its own keywords (Statuspage the external set, Cluster Sentinel the internal set), matched as whole words in the query and scope; a query that matches no source's keywords targets every source. The node holds no keyword sets or source names, so adding a source means writing one adapter and registering it with the probe.
- **`collect_telemetry`**: One `probe.collect(targeted_sources, query, service_filter)` call: the probe reads the targeted sources concurrently, each through breaker → source deadline → retry, and returns the signals plus the tripped breakers and unavailable sources (`docs/resilience.md` section 3).
- **`degraded_fallback`**: Invoked if any tripped breaker was recorded (a targeted source whose circuit breaker is `OPEN`). Annotates the incident with known blind spots, naming each tripped breaker by its source (`probe.describe(id)`, e.g. `Cluster Sentinel [chaos]`), and synthesizes available telemetry.
- **`synthesize`**: Merges telemetry signals and historical context.
- **`generate_report`**: Emits a structured candidate JSON triage report via the active LLM engine.
- **`route_after_generation`**: A completion goes to `validate_report`. A provider failure (`provider_failure` set) skips `self_repair`: in a refine round, `generate_report` has put the pre-refine report (`refined_from`) back in `final_report`, so the run goes to `finalize` with that report; otherwise it goes straight to `fallback_report`.
- **`validate_report`**: Markdown fence auto-cleaning (`clean_json_string`) and strict Pydantic validation. On failure, passes exact error paths to `self_repair`.
- **`self_repair`**: Feeds `ValidationError` details back to the LLM context to correct malformed fields (max 2 retries).
- **`fallback_report`**: Constructs a deterministic safe report after a provider failure or once self-repair retries are exhausted. Its root cause follows `provider_failure`: `RateLimited` names the rate limit, `Unavailable` and `MalformedResponse` the unavailable provider, `Rejected` the rejected request, and `None` the exhausted self-repair.
- **`finalize`**: Runs **report reconciliation** (`reconcile` in `graph/reconciliation.py`) on the report, whichever path produced it. It does not write to the cache; the Triage gateway stores the report. It is one of the two call sites that set a report's `degradation_status` and `circuit_breakers_tripped`; the other is the gateway's emergency path (below).

### Report reconciliation

`collect_telemetry` records `tripped_breakers` in state: the *targeted* sources whose breaker is `OPEN` at collection time. An open breaker the query never targets does not count. It also records `unavailable_sources`: targeted sources whose read failed while their breaker was not tripped. `fallback_report` records `synthesis_fell_back: True`. `finalize` builds a `DegradationEvidence` from that state and calls the pure `reconcile(report, evidence)`, which returns a copy with:

- `circuit_breakers_tripped` = the sorted tripped-breaker set. Whatever the LLM wrote is discarded.
- `degradation_status` = the maximum (HEALTHY < PARTIALLY_DEGRADED < SEVERELY_DEGRADED) of: synthesis fallback ⇒ SEVERELY_DEGRADED; two or more tripped breakers ⇒ SEVERELY_DEGRADED; exactly one ⇒ PARTIALLY_DEGRADED; any unavailable source ⇒ PARTIALLY_DEGRADED; otherwise HEALTHY. The LLM's value is discarded and `UNKNOWN` is never produced.

**Approval rule.** `reconcile` also sets `requires_approval=True` on every recommended action whose `action_type.strip().lower()` is not `"diagnostic"`, so an unexpected label such as `"Remediation"` or `"mutating"` fails safe. The rule only raises the flag, never lowers it, and is the only code that writes `requires_approval`; `validate_report` just parses and fills defaults.

**Second call site: the emergency path.** If the graph returns no `final_report`, the Triage gateway (`triage/gateway.py`) builds one with `build_fallback_report`, never reaching `finalize`. It then calls `reconcile` itself, on the graph's evidence with `synthesis_fell_back: True`, and returns it without storing it, so that report is `SEVERELY_DEGRADED` and lists any tripped targeted breakers.

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
   - `StatuspageSource` caches parsed summaries for 10 seconds in-memory, eliminating transcontinental HTTPS latency (**~650ms – 700ms saved per query**).
2. **Persistent Groq HTTP Connection Pooling & Token Budget**:
   - Replaced per-request HTTP client creation with a persistent, keep-alive client pool (`httpx.Limits(max_keepalive_connections=LLM_MAX_KEEPALIVE_CONNECTIONS, max_connections=LLM_MAX_CONNECTIONS)`, defaults 20 and 50).
   - Caps the Groq LPU payload at `max_tokens=LLM_MAX_TOKENS` (default 2500) to stop runaway token generation while leaving room for a complete, structured incident report.
   - Eliminates ~200ms of TCP/TLS handshake latency and ~300ms of generation time.
3. **In-Memory LRU Vector Cache (512 Entries) & Preset Pre-Warming**:
   - Added an `OrderedDict` LRU cache to both `FastEmbedProvider` and `DeterministicEmbeddingProvider`.
   - All quick presets (`Actions 503`, `Stripe Timeout`, `Postgres Pool`) are pre-warmed during FastAPI startup (`lifespan`).
   - Lookups, stores, and preset re-runs execute in **0.001ms** instead of 100–250ms on shared vCPU environments.

---

### 8. Real-Time Groq Quota Monitoring & Model Failover
- **Problem**: Groq free tier imposes a strict 1,000 output tokens/minute limit on `qwen/qwen3.8-27b`. Bursts of incident triage requests can hit HTTP 429 mid-incident.
- **Header Telemetry**:
  - `GroqAdapter` reads the rate-limit headers (`x-ratelimit-remaining-tokens`, `x-ratelimit-limit-tokens`, `x-ratelimit-reset-tokens`, and the `-requests` set) on every response and keeps them per model.
  - The gateway counts adapter calls, 429 rate-limit hits and failover events.
  - `LLMGateway.snapshot()` returns the strict typed [`LLMQuotaSnapshot`](schemas.md#llmquotasnapshot) served by `GET /llm/quota` and embedded in `GET /health` and `GET /resilience/status`. Its budget fields describe the primary model only.
- **Secondary Model Failover**:
  - A 429 from the primary puts it into a cooldown sized by the rate limit's scope, and the same prompt goes to the fallback model, which has its own quota bucket, unless the fallback is cooling down too. While the primary cools down, calls go straight to the fallback; a model in cooldown is never called, so while both cool down `complete()` makes no call and returns `RateLimited`.
  - Each `complete()` makes at most 3 calls. The full policy is in the [resilience guide](resilience.md#6-llm-gateway-failover-and-rate-limit-cooldowns).
- **Diagnostic Logging**:
  - Every failed call logs its model and failure kind at `WARNING`, and the provider's error message is kept as the snapshot's `last_error`.

