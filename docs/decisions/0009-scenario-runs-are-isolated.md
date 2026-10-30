# ADR-009: Scenario Runs Are Fully Isolated from the Shared Runtime

## Status
Accepted

## Date
2026-10-31

## Context
The UI rebuild (map issue #86) is a public explorable explanation, so strangers click
concurrently and nothing one visitor does may change what another sees. It has two kinds of run
(glossary): a **scenario run** (`POST /scenarios/{id}/stream`, empty body) replays a catalogued
scenario whose model replies and Statuspage readings are scripted, and **free play** is today's
`POST /triage` / `POST /triage/stream` with real Statuspage and the shared Redis cache. Free play
uses real Groq when `GROQ_API_KEY` is set and today's keyless simulation otherwise, as the
[#91 resolution](https://github.com/SamSilmarilData/resilient-triage/issues/91#issuecomment-6527237734)
settled. There is no demo mode and no `DEMO_MODE` setting.

Scenarios must show faults (a rate-limited model, Statuspage failing, Cluster Sentinel slow, Redis
down, an already-tripped breaker), and each must repeat exactly whatever other visitors did. But
today every fault is global per `AppRuntime`: chaos injection, breaker state, model cooldowns and
the cache are shared by every request, and `TriageDeps` is shared across runs. The mutating admin
routes (`/chaos/inject`, `/resilience/reset`, `/cache/clear`) are open whenever `ADMIN_API_KEY` is
unset, and the old page called them with no key.

This ADR records decisions already made in the resolutions of
[Demo mode: enabling, per-run scripting, cache isolation and labelling](https://github.com/SamSilmarilData/resilient-triage/issues/91#issuecomment-6527237734)
(#91, which proposed the private-cache ADR candidate folded in here),
[Fault hooks and public access to admin-protected controls](https://github.com/SamSilmarilData/resilient-triage/issues/92#issuecomment-6532738821)
(#92, which asked for this ADR),
[Scenario catalogue](https://github.com/SamSilmarilData/resilient-triage/issues/93#issuecomment-6532825762)
(#93) and
[Visual prototype of the explorable page](https://github.com/SamSilmarilData/resilient-triage/issues/96#issuecomment-6534470859)
(#96). Where this text and a resolution differ, the resolution wins.

## Decision
**Every scenario run gets its own fully private set of collaborators. The shared `AppRuntime` is
never read or mutated by a scenario run, and faults exist only in scenario runs.**

- **One per-run factory.** For each scenario run, one factory builds:
  - a scripted `LLMGateway` around a fresh `ScriptedAdapter`, so cooldowns start clean and
    failover repeats exactly;
  - a `TelemetryProbe` whose Statuspage source (`id = "statuspage"`) is canned, returning the
    scenario's fixed `StatuspageSummary` with no HTTP call, or failing, plus a private
    `ChaosManager` for Cluster Sentinel;
  - a fresh `CircuitBreakerRegistry`;
  - an in-memory `SemanticCacheManager`;
  - a `TriageGateway` around them.

  They reach the graph as run-scoped `TriageDeps` (`dataclasses.replace(deps, ...)`, the seam
  ADR-008 uses for the recorder). There are no module-level singletons. The graph, routers,
  reconciliation, validation, cache lookup and store, failover and breaker code all run for real.
- **Scenario faults.** LLM faults (primary rate-limited, provider unavailable, malformed output)
  are entries in the scenario's script, handled by the real gateway's failover and cooldowns,
  so they need no separate hook. Statuspage failing means the canned source raises, so the run
  sees an *unavailable source*. Cluster Sentinel failing or slow is set on the private
  `ChaosManager`. Redis down means the private cache gets a `remote_store_factory` whose store
  fails to connect, so the real in-memory failover runs.
- **Scenario seed.** A scenario that must hit the cache stores a query and report through the
  real `store()` path before the run. The lookup then embeds and compares similarity for real.
  The cache key and index format are untouched.
- **Breakers.** An already-open breaker is reached by recording `fail_max` real failures on the
  private breaker before the run, shown as *scenario setup* ("Statuspage failed 3 times before
  this run; its breaker is open"). We never fake an open state or lower `fail_max`.
- **Request path and metrics.** The per-run `TriageGateway` runs the real cache lookup → run slot
  → graph → store path, including shedding and the run deadline. Its outcome counts are thrown
  away, so scenario runs drop out of `/metrics` with no special case. Scenario runs never use the
  shared gateway, so they are also absent from `/llm/quota`. Private breakers never appear in
  `/resilience/status`.
- **Scenario-only overrides.** Two scenarios declare an override as scenario setup, shown on the
  page:
  - *Run deadline*: a short run deadline (3 s in the catalogue) for that run only.
  - *Shed request*: a private slot pool whose slots are already held, with a short queue
    timeout. It is the one exception to the shared scenario slot pool. The real queue wait and
    timeout still run.
- **Scripted reply delay.** Each scenario's script declares a delay per reply (the prototype used
  0.9 s for a reply and 0.18 s for a failure), so the waterfall shows real proportions. The delay
  is a real `asyncio.sleep` inside the run, so the run deadline and timings stay true.
- **Abuse limits.**
  - Scenario runs take slots from one shared **scenario slot pool**
    (`scenario_max_concurrent_runs = 4`, reusing `triage_queue_timeout_s`), injected into each
    per-run `TriageGateway`, so scenario traffic and free play cannot shed each other.
  - Free play has one deployment-wide budget, `free_play_runs_per_minute = 20`, counting graph
    runs only (cache hits are free). Over budget, the request is a *shed request* with
    retry-after. Shed request covers both causes, with the reason on the trace event, not as a
    new outcome.
  - No per-IP limits. No new dependencies.
  - Free play takes no faults: it stays "answered exactly as any triage request is". There is no
    compose-a-scenario control; the page offers only the fixed catalogue, and visitors can never
    change global state.
- **Admin routes.** `/chaos/inject`, `/resilience/reset` and `/cache/clear` stay behind
  `X-Admin-Key` and act only on the shared runtime.
  - They **fail closed** (403) when `ADMIN_API_KEY` is unset. Local dev sets a key in `.env`;
    tests and the smoke test must set one.
  - The check moves to `fastapi.security.APIKeyHeader`, so `/docs` has an "Authorize" button.
    That is the operator path.
  - The public page has no operator controls, which fixes the old page's 401 bug by removing the
    caller that sent no key.
  - Operator chaos injection stays global and may degrade free play while it is on. That is the
    operator's deliberate act, not another visitor's.

## Considered Options
- **A global demo mode** (a `DEMO_MODE` setting that scripts the whole deployment). Rejected:
  every visitor would share one script, cooldowns, breakers and cache, so runs would not repeat
  and one visitor's run would change another's. It also makes the real-model path a different
  deployment rather than a different run.
- **Faking breaker state** (forcing a breaker open or lowering `fail_max` for a scenario).
  Rejected: the page claims everything shown is real. Recording `fail_max` real failures on a
  private breaker exercises the same transition code and is shown honestly as setup.
- **Shared Redis with namespaced keys per run.** Rejected: it changes the cache key/index
  contract (`triage_vector_idx`, `triage:doc:*`), leaves scenario data in the shared store, and a
  scenario's hit or miss would depend on Redis being up. A private in-memory cache with a seed is
  repeatable whatever other visitors did.
- **Per-IP rate limits.** Rejected: behind a proxy the client IP comes from spoofable headers.
  One deployment-wide free-play budget and a separate scenario slot pool bound the cost instead,
  with no new dependency.

## Consequences
- Redis itself is not used in scenario runs. Redis down is shown by a failing
  `remote_store_factory` on the private cache, and appears only in the finale scenario; a single
  run shows it only as a cache-backend trace event.
- `/metrics`, `/llm/quota` and `/resilience/status` describe real traffic only. A scenario run's
  breaker transitions and model calls are visible only on its own trace.
- Each scenario run builds its own collaborators. The FastEmbed embedder and the compiled graph
  stay process-wide, so the cost is a few small objects per run.
- A scenario can be added as data (script, canned Statuspage, setup) with no new hook, as long as
  its faults come from the set above.
- A deployment without `ADMIN_API_KEY` has no working admin routes. That is a breaking change for
  anyone relying on the open default, and must be called out in the changelog when it ships.
- Scripted delays slow scenario tests, so an autouse fixture zeroes them and one test opts out.
