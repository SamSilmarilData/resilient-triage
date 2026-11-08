# ADR-008: Execution Trace from a Per-Request Recorder, Streamed over SSE

## Status
Accepted

## Date
2026-10-30

## Context
The UI rebuild (map issue #86) shows what really happened during a triage request: which nodes
ran and for how long, which way each router went and why, every model call and failover, every
telemetry read and breaker transition, every reconciliation change, and how the request ended.
Much of that never reaches `TriageState`. Model choice, failover, cooldowns, retries and breaker
transitions live in `LLMGateway` and `TelemetryProbe`. Cache lookup, the run slot, the run
deadline and the cache store live in `TriageGateway`, outside the graph. The deployment is public,
so concurrent strangers must each see only their own request, and nothing on the wire may leak a
prompt, model text or another visitor's query. The streaming facts this decision relies on are
in "LangGraph streaming and FastAPI SSE for an execution trace" (issue #88).

## Decision
Every triage request gets its own **execution trace**, recorded by an explicit `TraceRecorder`
that is the trace's only source.

- **Recording.** `TriageGateway.triage(..., trace=recorder)` records the gateway's work. The graph
  still runs with `ainvoke`, with `dataclasses.replace(deps, trace=recorder)` as its context.
  `TriageDeps.trace` defaults to `NullTrace`. `builder.py` wraps each node to record its start,
  finish and timing. Nodes pass `runtime.context.trace` explicitly to `LLMGateway.complete` and
  `TelemetryProbe.collect`, whose `trace` parameter defaults to `NullTrace`, so callers outside
  a run (`/chaos/*`) are unchanged.
- **Router decisions** are derived, not instrumented: each consecutive pair of nodes maps to its
  unique `get_graph()` edge label, and a test fails if a pair ever becomes ambiguous. The reason
  for a decision is composed from the trace events that came before it. Routers stay pure.
- **The recorder** (`src/resilient_triage/trace/`) has a synchronous `record(event)` that never
  raises, and it stamps `seq` and `t_ms` (monotonic ms since the request started). The clock
  starts when `trace_started` is recorded, so it is stamped 0; a scenario run's setup, recorded
  before it, is stamped 0 too and ordered by `seq`, so setup time never counts as run time. Its
  implementations are `NullTrace`, `ListTrace` (which builds the trace summary) and
  `StreamingTrace` (a `ListTrace` that also feeds an unbounded `asyncio.Queue`, so a slow
  client can never block a run and no event is dropped).
- **Trace events** are frozen Pydantic models in a discriminated union on `kind`:
  `trace_started`, `cache_lookup`, `run_slot`, `node_started`, `node_finished`, `route_decided`,
  `model_call`, `telemetry_read`, `validation_failed`, `reconciled`, `deadline_exceeded`,
  `cache_store` and `trace_finished`.
- **No free text on the wire.** Events carry ids, kinds, counts and timings. They never carry
  the query text, prompts, model output, signal snippets, or Pydantic error `input` values. The
  report travels once, inside `trace_finished`.
- **Delivery.** `POST /triage/stream` takes the `TriageRequest` body and returns a
  `fastapi.sse.EventSourceResponse`. Each event's SSE `event:` is its `kind` and its `data:` is
  the JSON DTO. Every stream ends with `trace_finished`, which carries the triage outcome and
  the same `TriageResponse` that `POST /triage` would return (none for a shed request). The
  browser reads it with `fetch()` and a small parser.
- **Trace summary.** `TriageResponse` gains an optional, structured `trace: TraceSummary`
  (node path, route keys, model calls, failovers, per-source telemetry outcomes, repair and refine
  rounds, reconciliation changes, timings). Prose renderings of it belong to the frontend.
  `IncidentTriageReport` is unchanged.
- **Failure on the stream.** Once the 200 headers are sent, an error can't become a 500. The
  generator catches `Exception` (never `BaseException`) and still ends with `trace_finished`
  carrying a reconciled synthesis fallback. A run deadline is recorded as `deadline_exceeded`
  and then answered as before.
- **Abandoned requests.** A client disconnect cancels the run. `CancelledError` propagates, the
  run slot is released in `finally`, and nothing is cached. An abandoned request is not a triage
  outcome. It is logged and counted under its own `abandoned` metric.

## Considered Options
- **`astream(stream_mode=["tasks", "custom"])` with `get_stream_writer()` in collaborators.**
  Rejected: `get_stream_writer()` raises `RuntimeError` outside a graph run, which would break
  the never-raises contract of `LLMGateway` and `TelemetryProbe` when `/chaos/*` calls them. The
  documentation says `tasks` mode needs a checkpointer; it only works without one (ADR-007) as
  undocumented behaviour. And no stream mode sees the gateway's work, which happens outside the
  graph.
- **Diffing `TriageState` between steps.** Rejected: it misses everything that lives outside
  state (failover, cooldowns, retries, breaker transitions, the cache, run slots).
- **A run id, then `GET` with the browser's `EventSource`.** Rejected: it needs run state held
  across requests, which ADR-007 rules out. For the same reason there is no `Last-Event-ID`
  resume.
- **A bounded queue that drops events.** Rejected: a run emits a few dozen events within its run
  deadline, so the bound buys nothing and a gap would make the trace untrue.

## Consequences
- A dropped connection loses its trace. The caller re-sends the request, which may now be a
  cache hit.
- `TriageResponse` grows an optional field. Clients that ignore unknown fields are unaffected.
- `schema_version` (sent in `trace_started`) changes only when a kind or field is renamed or
  removed. Adding a kind or an optional field is not a version bump, so clients ignore kinds
  they don't know.
- Adding a node or conditional edge means the recorder's wrapper and the edge-uniqueness test
  cover it automatically, and a test fails if it creates an ambiguous (node, node) pair.
- The full event table (each kind's fields) is documented in `docs/api.md` when it is built, not
  here.

## Implementation notes
- **Route pairs.** The (node, node) pair is read from `TriageState.previous_node`, a key the
  node wrapper writes into each update. Routers stay pure and no state is diffed.
- **`telemetry_read.attempts`** is counted inside the retried call, not in tenacity's
  `before_sleep`, so it stays accurate when the run deadline expires mid-backoff.
