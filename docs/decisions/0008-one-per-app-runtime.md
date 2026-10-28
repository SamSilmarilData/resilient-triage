# ADR-008: One Per-App Runtime, No Module-Level Singletons

## Status
Accepted

## Date
2026-10-28

## Context
Stateful collaborators lived as module-level singletons (`semantic_cache`,
`circuit_breaker_registry`, `chaos_manager`, `groq_quota_tracker`), plus hidden module state
(the Statuspage summary cache and httpx client, the Groq httpx client, the LLM override).
Graph nodes imported them directly. The conftest autouse fixture reset each one by hand
before and after every test, and every new singleton had to be added there too, so a missed
reset leaked state between tests. `/health`, `/resilience/status` and `/metrics` each probed
the singletons separately (see "One runtime object for the module-level singletons", issue #19).

## Decision
- `AppRuntime` holds the app's stateful deep modules: the semantic cache, the breaker registry,
  the chaos manager, the LLM gateway, the telemetry probe, and the triage gateway built from them.
  Any state that would need its own reset lives inside one of those modules.
- `build_runtime(settings, *, llm_adapter=None, embedder=None)` is pure construction.
  `await runtime.start()` (cache init, warm-up, default breakers) and `await runtime.aclose()`
  never raise. `await runtime.snapshot()` returns one `ResilienceSnapshot` that the status
  routes map onto their existing response models.
- `create_app(runtime_factory=build_runtime)` builds the runtime in `lifespan` and stores it on
  `app.state.runtime`. Routes reach it with `Depends(get_runtime)`. `app = create_app()` stays
  for uvicorn.
- Graph nodes get their collaborators through LangGraph's run context:
  `context_schema=TriageDeps`, with nodes typed `(state, runtime: Runtime[TriageDeps])`.
  `TriageDeps` has three fields: the LLM gateway, the telemetry probe, and `history`, a read-only
  `CacheReader` protocol (`lookup` only) that `compensate_blindspots` uses. Nodes can read cache
  history but can't write or clear it, so the triage gateway stays the only cache writer. The
  graph is compiled once and stays stateless.
- Tests get a **fresh runtime** per test instead of a reset. There's no `AppRuntime.reset()`.
  The admin reset routes call the modules' own resets.
- Stateless or expensive resources stay process-wide: `settings`, the FastEmbed model and its
  LRU, and the compiled graph.

## Considered Options
- **A module-level `runtime` global swapped out in tests.** Rejected: it keeps the
  "remember to reset it" hazard, just with one object instead of six.
- **Build the graph per runtime, binding nodes with `functools.partial`.** Rejected: it
  recompiles the graph for every runtime, and LangGraph's run context is the library's own seam.
- **Nodes call a global `get_runtime()`.** Rejected: it hides the dependency, so a node test
  can't pass a fake.

## Consequences
- The `langgraph` floor rises to `>=1.0,<2` for `context_schema` / `Runtime` injection. This is a
  pin change, not a new dependency.
- Every node's signature changes from `(state)` to `(state, runtime)`.
- The CLAUDE.md rule "any new module-level singleton must be reset in conftest" is replaced by
  "stateful modules are members of `AppRuntime`; don't add module-level singletons".
