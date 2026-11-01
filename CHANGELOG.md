# Changelog

All notable changes to the `resilient-triage` project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`POST /triage` returns a trace summary** (#110, ADR-008): the response has a new optional `trace` field, a small structured `TraceSummary` of how the request was handled: node path with durations, router keys taken, model calls and failovers, per-source telemetry outcomes, repair and refine rounds, reconciliation changes, degradation, whether the run deadline passed, and timings (total, cache lookup, slot wait, run, model). It never carries the event list or free text. `IncidentTriageReport`, headers, caching and `/metrics` counts are unchanged; clients that ignore unknown fields are unaffected. See `docs/api.md`.
- **`POST /triage/stream`** (#111, ADR-008): the same triage as `POST /triage` (same body), streamed as Server-Sent Events while it runs. Each trace event is one SSE event (`event:` its kind, `data:` its JSON), with a `: ping` every 15 s of silence. Every stream ends with exactly one `trace_finished` carrying the outcome and the `TriageResponse` `POST /triage` would return (none when shed); the status is 200 once streaming starts, including on a shed, a run deadline or an unexpected exception (answered with an uncached synthesis fallback). No run id, no `GET`, no resume. See `docs/api.md`.
- **Scenario factory** (#112, ADR-009): `ScenarioFactory` builds each scenario run on its own scripted model gateway, canned or failing Statuspage, chaos manager, breakers, in-memory cache and Triage gateway, with traced scenario setup (cache seeds, breakers tripped by real failures, held run slots) and per-run overrides (run deadline, a pre-filled slot pool). The run's clock starts at `trace_started`: every trace event's `t_ms` counts from it, and setup events, recorded before it, are stamped 0, so setup time (real retry waits when tripping a breaker) never counts as run time. The Triage gateway runs the setup (`triage(..., scenario_id=, setup=)`), so its events stream live, and a setup that raises is answered with an uncached synthesis fallback, never a 500. Scripted replies can declare a delay (`ScriptedReply`). No route serves it yet; free play, `/metrics`, `/resilience/status` and `/llm/quota` are unchanged. See `docs/architecture.md`.
- **Scenario catalogue** (#113): `scenarios/catalogue.py` holds the 13 scenarios in four chapters (fast paths, the model misbehaving, dependencies failing, everything at once), each with a title, caption, setup notes, a script with prototype delays (0.9 s per reply, 0.18 s per failure) and the outcome its run must show (node path, routes, model calls, source reads, degradation, key report facts). `Scenario.public_view()` is the page's view of one (id, chapter, title, caption, setup notes); `GET /scenarios` serves it (#114). See `docs/architecture.md`.
- **Scenario routes** (#114, ADR-009): `GET /scenarios` returns the catalogue's public view (`ScenarioCatalogue`: chapters in display order, each with its scenarios' id, title, caption and setup notes). `POST /scenarios/{scenario_id}/stream` (empty body; 404 on an unknown id) runs one scenario on private collaborators and streams its trace like `POST /triage/stream`, setup first. Scenario runs share their own pool of run slots, separate from free play's, so neither sheds the other, and they never change `/metrics` outcome counts, `/llm/quota` or `/resilience/status`. New setting, in `.env.example`:
  - `SCENARIO_MAX_CONCURRENT_RUNS` (default 4, 1 to 100): scenario runs in flight per app; they wait up to `TRIAGE_QUEUE_TIMEOUT_S` for a slot.
- **Free-play run budget** (#114): free play admits at most `FREE_PLAY_RUNS_PER_MINUTE` graph runs in any sliding 60 s window. Cache hits are free; a cache miss or `force_refresh` run counts once admitted, even if it is then shed for lack of a slot. A run over the budget gets a 503 with a `Retry-After` header (seconds until the oldest counted run ages out), and counts as `saturated` on `/metrics` (no new outcome). The `run_slot` trace event gains `reason` (`capacity` / `budget`) when shed and `retry_after_s` on a budget shed. The budget is per process: with several workers each allows the full amount. See `docs/resilience.md`. New setting, in `.env.example`:
  - `FREE_PLAY_RUNS_PER_MINUTE` (default 20, 1 to 10000).
- **Abandoned requests metric**: a triage request cancelled before it ends (a client disconnect) has its run cancelled, its slot released and nothing cached, and is counted exactly once in a new `/metrics` counter, `resilient_triage_abandoned_requests_total`, whether the cancel lands before the run starts, during it, or during the cache store (a store cancelled mid-write to Redis evicts its entry again, and a run whose graph swallowed the cancel is not stored). It is not a triage outcome, so the `resilient_triage_requests_total` counts are unchanged.
- **Vendored frontend assets at `/static`** (#115, ADR-010): the app serves `src/resilient_triage/api/static/` at `/static`, and it now ships in the wheel and the Docker image (package-data `static/**`; the build needs setuptools 62.3+). `static/vendor/` holds Preact 10.29.8 (+ hooks), htm 3.1.1 (+ `htm/preact`), `@preact/signals` 2.11.4, `@preact/signals-core` 1.14.5 and the Inter and JetBrains Mono variable woff2 Latin subsets, each with its LICENSE and pinned by sha256 in `static/vendor/manifest.json`. `.mjs`/`.js` are served as `text/javascript` and `.woff2` as `font/woff2` whatever the host's MIME database holds; `/static/vendor/` files carry `Cache-Control: public, max-age=31536000, immutable` and other static files `no-cache`. The new stdlib-only `scripts/vendor.py` has `verify` (offline, also run by the test suite and on the CI image with `--installed`) and `update` (manual, downloads from npm). The page shell at `/` (below) is built from them. See `docs/api.md` and `docs/deployment.md`.
- **Page shell at `/`** (#116, ADR-010): `/` now serves `static/index.html` (`Cache-Control: no-cache`), the shell of the explorable page: the hook, guided scenarios, free play and "how it's built" sections as static prose with a `<noscript>` note, and empty states for the live trace, chapters and free play rendered by `static/app.mjs` (Preact, htm, Signals through one same-origin import map). `static/styles.css` has OKLCH tokens, a dark theme that follows the system or `<html data-theme>`, reduced-motion support and the self-hosted fonts. `static/graph-layout.json` is the hand-placed top-to-bottom graph layout, kept equal to `get_graph()` by a two-way test. The page makes no third-party requests. See `docs/architecture.md` and `docs/api.md`.
- **Browser test tier** (#117, ADR-010): `tests/browser/` drives the page in headless Chromium through Playwright, against the app served on loopback over a scripted runtime (free play never reaches Groq), with every request off the app's own origin aborted and failing the test. It checks the page shell loads with no console errors and no failed or off-origin requests, has no serious or critical axe-core violation in light and dark, and doesn't scroll sideways at 375 px. Tests are marked `browser` and deselected by default; `./scripts/check.sh --browser` runs only them and fails when Chromium is missing. New optional extra `browser` (`playwright>=1.49`; then `playwright install chromium`), and a new CI job, `browser`, with cached browsers. axe-core 4.14.0 is vendored test-only under `tests/vendor/`, pinned by sha256, never served or shipped. See `docs/deployment.md`.
- **Docker-free package check** (#104): `scripts/check_package.sh` (or `./scripts/check.sh --package`) builds the wheel, installs it non-editable into a fresh temporary venv, runs `scripts/vendor.py verify --installed` from outside the checkout, boots the installed app with uvicorn on a free loopback port and runs `scripts/smoke_test.py` against it, then cleans up. It needs no Docker, Redis or Groq key; the image's OS layer and non-root user are still checked only by CI's docker job. See `docs/deployment.md`.

### Changed
- **The SRE console at `/` is gone** (#116): the old single-page console (`api/templates/index.html`, with Tailwind's Play CDN script, its chaos buttons and preset queries) is deleted, with its package-data entry; `/` serves the new page shell instead. The API routes it called are unchanged; use `/docs` for them until the new page's scenarios and free play land.
- **Requires FastAPI 0.135.1 or later** (was 0.110.0): `POST /triage/stream` uses `fastapi.sse` (added in 0.135.0) and relies on 0.135.1's SSE teardown on the request exit stack.
- **Declares `anyio>=4.0,<5`** as a direct dependency (already installed with FastAPI): the stream route catches its `BrokenResourceError` on a mid-send disconnect.
- **A failing cache store no longer fails `POST /triage`**: if storing the run's report raises, the Triage gateway logs it and still answers 200 with the report.
- **`resilient_triage.schemas` no longer re-exports its models**: import them from `schemas.api`, `schemas.incident`, `schemas.state` or `schemas.telemetry`.
- **Breaking: admin routes fail closed** (#107): `POST /chaos/inject`, `/resilience/reset` and `/cache/clear` now answer `403` when `ADMIN_API_KEY` is unset or empty, where they used to admit everyone. With a key set, a missing or wrong `X-Admin-Key` still gets `401`, and the key is compared in constant time. **Set `ADMIN_API_KEY`** (in `.env` locally; `.env.example` has a placeholder) to keep using them. The header is now a `fastapi.security.APIKeyHeader` security scheme (`AdminKey`), so `/docs` has an **Authorize** button. `scripts/smoke_test.py` sends `ADMIN_API_KEY` (or `--admin-key`) and gives a server it launches a random key; `docker-compose.yml` passes `ADMIN_API_KEY` through. The old SRE console, whose chaos buttons sent no key, is gone in this release (above).

## [2.0.0] - 2026-10-30 — Deep Modules: Gateways, Guarded Probe & One Runtime

HTTP routes, response models, the `IncidentTriageReport` schema and the cache key/index format are unchanged. This is a major release because of the Python and configuration removals below: `OPENAI_API_KEY` / `GOOGLE_API_KEY` / `GEMINI_API_KEY` no longer select a provider, the LangChain model classes and `BoundedMemorySaver` are gone, module-level singletons are replaced by `AppRuntime`, and a refused breaker call raises `BreakerOpen` instead of `pybreaker.CircuitBreakerError`.

### Changed
- **The guarded telemetry probe picks the Targeted sources**: `analyze_query` now asks `TelemetryProbe.select_targets(query, scope)` and holds no keyword sets. Each Telemetry source carries its keywords unchanged (Statuspage the external set, Cluster Sentinel the internal set), still matched as whole words; a query matching no source targets every source. The state key `target_tools` is renamed `targeted_sources` and is now sorted, so a query targeting both lists `["chaos", "statuspage"]` (same set as before). `degraded_fallback` names tripped breakers through the probe (`describe`): Cluster Sentinel still reads `Cluster Sentinel [chaos]`, and Statuspage now reads `Statuspage [statuspage]` instead of `statuspage`. No source name or keyword is left in `graph/`, so adding a source means writing one adapter and registering it with the probe.
- **One guarded telemetry probe reads every Telemetry source**: `TelemetryProbe` (`telemetry/probe.py`), built by `build_runtime` and reached by nodes as `TriageDeps.telemetry`, owns the breaker → source deadline → retry → read order, concurrent reads, the Tripped/Unavailable decision and one degraded signal per failed source. Statuspage and Cluster Sentinel are now adapters that only read and parse (`StatuspageSource`, `ClusterSentinelSource`); `StatuspageClient`, `ChaosClient`, the public `resolve_probe_source` and the `TelemetrySources` bundle are removed. `collect_telemetry` makes one `collect()` call and no longer inspects signal statuses or source names. The probe creates the `statuspage` and `chaos` breakers when it is built, so `AppRuntime.start()` no longer registers them; `/health` still lists both from startup. One visible change: the read that trips a source's breaker now reports `CIRCUIT_OPEN` (still naming the real error) instead of `UNAVAILABLE`, matching its place in `tripped_breakers`. Routes, response models and breaker names are unchanged. See `docs/resilience.md` section 3.
- **A slow telemetry source can no longer stall a triage** (bug-hunt finding 8): every Statuspage and Cluster Sentinel read, retries included, now runs under a source deadline that sits inside the source's breaker (breaker → deadline → retry → read). Running out of time counts as one breaker failure and the source is reported unavailable for the run, with its usual degraded-signal label. A cancellation from outside the read still records nothing. `GET /chaos/probe` runs under the same deadline and answers 503 when it passes. Statuspage's httpx timeouts stay the inner limit. See `docs/resilience.md` section 3. New setting, in `.env.example`:
  - `TELEMETRY_SOURCE_DEADLINE_S` (default 5.0s, 0.5 to 30): the source deadline.
- **`ChaosConfig.latency_ms` is capped at 30 000 ms**: `POST /chaos/inject` with a larger `latency_ms` now returns 422.
- **A stalled triage run no longer holds its slot or client indefinitely**: every graph run the Triage gateway starts now has a run deadline. When it passes, the run is cancelled, its slot released, and `POST /triage` answers 200 (`X-Cache: MISS`) with a `SEVERELY_DEGRADED` synthesis fallback report ("Run deadline exceeded"). That report is not cached and `/metrics` counts it as a `miss`. Slot wait does not count against the deadline, and a client disconnect still cancels the run as before. See `docs/resilience.md` section 5. New setting, in `.env.example`:
  - `TRIAGE_RUN_DEADLINE_S` (default 45.0s, above 0 and at most 120): the run deadline.
- **A graph exception no longer gives `POST /triage` a 500**: if the triage graph raises anything except `CancelledError`, the Triage gateway logs it and answers 200 (`X-Cache: MISS`) with a `SEVERELY_DEGRADED` synthesis fallback report. That report is not cached, and the run slot is released. See `docs/resilience.md` section 5.
- **The `POST /triage` request path lives in the Triage gateway**: `TriageGateway` (`triage/gateway.py`, an `AppRuntime` member) does the cache lookup, waits for a run slot or sheds the request, runs the graph, and stores the report; the route only maps its outcome to HTTP. Responses, headers, caching and `/metrics` output are unchanged. The gateway is the only request-path cache writer: `finalize` no longer stores. The run-slot limit and wait are now settings, both in `.env.example`:
  - `TRIAGE_MAX_CONCURRENT_RUNS` (default 10, 1–100): graph runs in flight per app, previously hard-coded.
  - `TRIAGE_QUEUE_TIMEOUT_S` (default 5.0s, above 0 and at most 30): how long a request waits for a run slot before a 503, previously hard-coded.
- **Report reconciliation owns the degradation fields**: `finalize` now runs `reconcile` (`graph/reconciliation.py`) on every report, so `degradation_status` and `circuit_breakers_tripped` follow one rule whichever path produced the report. The LLM no longer controls either field: its values, including breaker names it made up, are discarded.
- **Any non-diagnostic action now requires approval**: report reconciliation sets `requires_approval=True` on every recommended action whose `action_type` is not `diagnostic` (case and whitespace ignored). Previously only the exact value `remediative` was caught, so labels like `"Remediation"` or `"mutating"` slipped through. The check moved out of `validate_report`.
- **Only targeted breakers degrade a report**: an open circuit breaker for a telemetry source the query does not target no longer marks the report degraded or lists that breaker.
- **Circuit breakers sit behind a project-owned interface**: `AsyncCircuitBreaker` (`resilience/circuit_breaker.py`) is now the only module that imports `pybreaker`. A refused call raises the project's `BreakerOpen` instead of `pybreaker.CircuitBreakerError`, and the breaker surface is `call`, `name`, `state`, `info()` and `reset()`. The Statuspage and Cluster Sentinel sources degrade exactly as before. The `pybreaker` constraint is now `>=1.3.0,<1.5` (1.2.0 raised `TypeError` from an open breaker).
- **Each app instance owns one runtime**: `create_app()` builds the gateway, and its lifespan starts one `AppRuntime` (cache, breakers, chaos, LLM gateway, telemetry clients) and closes it on shutdown. Routes, responses and headers are unchanged. The graph concurrency limit (10) and the `/metrics` request counters are now per app, and `/metrics` reads the same resilience snapshot as `/health`, so it pings Redis once per scrape like the other status routes.
- **No module-level singletons remain**: `semantic_cache`, `circuit_breaker_registry`, `chaos_manager`, `groq_quota_tracker`, the shared Statuspage and Groq HTTP clients, the process-wide Statuspage summary cache, and `set_triage_llm` / `get_triage_llm` / `get_active_model_name` are removed, with their package re-exports and close/clear helpers. `StatuspageClient` now requires its HTTP client and keeps its own summary cache and breaker; `ChaosClient` requires its `ChaosManager`; the LLM gateway owns its adapter and HTTP client. Only `settings`, the embedder and `triage_graph` stay process-wide; everything else lives on `AppRuntime`.
- **Every model call goes through one LLM gateway** (ADR-006): the new `resilient_triage.llm` package replaces `graph/llm.py`. `LLMGateway.complete(messages)` returns a `Completion` or a typed provider failure (`RateLimited`, `Unavailable`, `Rejected`, `MalformedResponse`) and never raises except `CancelledError`. `GroqAdapter` makes one HTTP call to one model and reads the rate-limit headers; `ScriptedAdapter` is the test fake and the zero-config simulation. `build_llm_gateway` is the one place a provider is chosen, and `build_runtime(llm_adapter=...)` takes a model adapter. The gateway owns the failover policy, by the primary's result: a rate limit cools the primary down and calls the fallback model; `Unavailable` retries the primary once after a short jittered backoff, then calls the fallback; `MalformedResponse` calls the fallback; `Rejected` is returned at once. A `complete()` makes at most 3 calls and never calls a model that is cooling down; a cancelled call records no counters but keeps any cooldown its 429 started. Cooldowns are per model and sized by the rate limit's scope (`minute`: `retry-after`, else the tokens bucket's reset; `day`: the requests bucket's reset, else `retry-after`; otherwise the default), and every cooldown is capped. Model calls are no longer wrapped in `with_retry`. The quota snapshot's `remaining_*`, `limit_*`, `reset_*` and `last_status` now describe the primary model only, and `active_model` names the model that served the last completion. See `docs/resilience.md` section 6. New settings, all in `.env.example`:
  - `LLM_REQUEST_TIMEOUT_S` (default 20s, at most 120s): the Groq HTTP timeout, previously hard-coded.
  - `LLM_TEMPERATURE` (default 0.0, 0–2) and `LLM_MAX_TOKENS` (default 2500, 1–32768): the sampling temperature and completion-token cap sent with every Groq call, previously hard-coded.
  - `LLM_MAX_KEEPALIVE_CONNECTIONS` (default 20, 0–1000) and `LLM_MAX_CONNECTIONS` (default 50, 1–1000): the Groq HTTP client's connection-pool limits, previously hard-coded.
  - `GROQ_FALLBACK_MODEL` (default `llama-3.1-8b-instant`): the fallback model, previously hard-coded. ADR-006 supersedes ADR-005's `openai/gpt-oss-20b`.
  - `LLM_DEFAULT_COOLDOWN_S` (default 30s, 1–3600): the cooldown when a 429 gives no usable wait.
  - `LLM_MAX_COOLDOWN_S` (default 3600s, 1–86400): the cap on every cooldown.
- **Unavailable sources degrade a report**: a targeted telemetry source that fails (its fetch raised or returned an `UNAVAILABLE` signal) while its breaker is still closed now makes the report at least `PARTIALLY_DEGRADED`; previously the report could claim `HEALTHY`.

### Removed
- **The request-path checkpointer** (ADR-007): `build_triage_graph()` compiles without a checkpointer, and `BoundedMemorySaver` with the `enable_checkpointing` / `max_checkpoint_threads` parameters is deleted. Each run is self-contained, so `POST /triage` no longer leaks one checkpoint thread per request. `X-Thread-ID` is now only a correlation id: the caller's value, or a new UUID4, echoed back as before.
- **OpenAI and Gemini providers**: `OPENAI_API_KEY`, `GOOGLE_API_KEY` and `GEMINI_API_KEY` no longer select a provider, and the provider branches that needed `langchain_openai` / `langchain_google_genai` (never dependencies) are gone. Only `GROQ_API_KEY` selects a real model; without it the app runs the zero-config simulation.
- **The LangChain model classes**: `GroqChatModel`, `GroqQuotaTracker`, `MockTriageChatModel` and `build_triage_llm`, with `graph/llm.py` and its re-exports from `resilient_triage.graph`.

### Fixed
- A synthesis-fallback report is no longer written to the semantic cache (fixes bug-hunt finding 5). Previously a run that fell back to the heuristic report was cached, so every similar query got that degraded answer as an `X-Cache: HIT` until the TTL ran out, even after the provider recovered. `POST /triage` still answers with the fallback (200, `X-Cache: MISS`, a `miss` on `/metrics`), but the Triage gateway skips the store, so the next identical query runs the graph again. This covers the gateway's own fallback for a run with no final report too. Reports degraded only by telemetry are still cached, with the degraded TTL.
- A later, shorter 429 no longer cuts short a model's running cooldown (#73). When two calls were waiting on the primary, a `minute` 429 (e.g. 7s) arriving after a `day` 429 (e.g. 900s) replaced the 900s window, so the primary was called again after 7s with its daily quota still used up. Each 429 now keeps the later of the existing deadline and its own. Cooldown lengths are unchanged, and a 429 after a cooldown has ended still starts a fresh window.
- The LLM gateway now starts a model's cooldown the moment its 429 arrives, not when `complete()` returns (#62). Concurrent requests that start while another waits on the fallback no longer call the rate-limited primary, and a call cancelled after a 429 still leaves that model cooling. A cooling fallback is no longer called: if the primary fails while the fallback cools down, the primary's failure is returned, and if both models are cooling down no call is made and `complete()` returns `RateLimited(scope="unknown")` with the shorter remaining window, recording nothing. Requests already waiting on the primary when its first 429 arrives still get their own 429s.
- A provider failure on the first generation now goes straight to the fallback report instead of through `validate_report`: the graph routes on the gateway's typed failure (`provider_failure` in state, `route_after_generation`) rather than on `"LLM Provider Failure"` text and a forced `repair_attempts`. The fallback's root cause now follows the failure kind (`RateLimited`, `Unavailable`/`MalformedResponse`, or the new `Rejected` wording, "LLM provider rejected the request. Safe heuristic fallback engaged."), not a `"429"`/`"503"` match in error text, so a self-repair failure whose validation errors mention 429 no longer claims a rate limit.
- A refine round no longer discards a valid report when self-repair runs out. When a low-confidence report triggers refinement and the refined output keeps failing validation until `MAX_SELF_REPAIR_RETRIES` is reached, the run now finalizes the valid report from before the refine (`refined_from` in state) instead of the synthesis fallback. The kept report still goes through `finalize`, so its degradation status comes from the run's telemetry evidence. The refine round shares the first generation's repair budget, so the longest path still fits the recursion limit.
- A provider failure during a refine round no longer discards the valid report (fixes bug-hunt finding 1). When a low-confidence report triggers refinement and the LLM provider then fails (for example `Unavailable`), the run now finalizes the report from before the refine instead of a `SEVERELY_DEGRADED` synthesis fallback at confidence 0.20. `generate_report` puts `refined_from` back in `final_report` on a provider failure, and `route_after_generation` sends that case to `finalize`, so reconciliation still applies to the kept report. A provider failure on the first generation still goes to the fallback report.
- A semantic cache lookup with no `service_filter` no longer returns a report cached under a service scope. "No scope" is now a scope of its own, not a wildcard: in both the in-memory and RediSearch engines, a lookup reuses only an answer stored under exactly the same scope. RediSearch now fetches the 5 nearest neighbours and takes the best one in the exact scope. If all 5 are in other scopes, the lookup is a MISS and the request runs the graph.
- Clearing the semantic cache while Redis is down (`POST /cache/clear`) now reaches Redis, so entries no longer come back after a reconnect. `clear()` empties memory and tries to reconnect at once, even inside the 5-second reconnect cooldown. If Redis is still down, the clear stays pending: the next successful reconnect deletes every `triage:doc:*` entry before Redis serves a search or hydrates the in-memory cache. If that deletion fails, the cache stays on the in-memory standby and keeps the clear pending. A clear that fails while Redis is connected also stays pending: it runs on reconnect if Redis dropped, or on the next lookup or store if Redis rejected it, and Redis search is skipped until it succeeds. `/cache/clear` still returns "ok" and `clear()` never raises.
- The semantic cache no longer leaks a Redis connection pool each time Redis drops. Every drop (a failed search, store, clear, health ping or hydration) now closes the client and starts the 5-second reconnect cooldown, and a connection attempt that fails closes its client too. A `clear()` that connects before startup's `initialize()` no longer makes `initialize()` open, and leak, a second client. A Redis query error (for example a RediSearch syntax error) no longer disconnects Redis on writes either: the error is logged and the in-memory copy serves. A cached report from RediSearch that fails validation is now a MISS and is deleted, like an in-memory one.
- A corrupt in-memory cache entry no longer makes `POST /triage` return a 500. If a cached report no longer passes `IncidentTriageReport` validation (or isn't valid JSON), the lookup logs a warning, evicts that entry, and returns a MISS, so the request runs the graph and caches a fresh report.
- The in-memory cache now rejects any vector whose size differs from the embedding provider's dimension, whether it comes from `store` or from Redis hydration, so the vector never enters the in-memory index. Previously one such vector made every later in-memory lookup raise. `store` logs a warning and skips that report, and a query vector of the wrong size is a MISS.
- An embedding provider failure no longer makes `POST /triage` return a 500. The semantic cache now logs a warning and degrades: `lookup` returns a MISS and `store` skips caching the report, so the request runs the graph as usual.
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
