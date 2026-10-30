# LangGraph streaming and FastAPI SSE for an execution trace

Research for issue #88 (map #86). It feeds the trace-design ticket #90.

**Versions checked (installed in `.venv`):** langgraph 1.2.16, langchain-core 1.7.0,
fastapi 0.161.1, starlette 1.9.0, uvicorn 0.54.0, anyio 4.15.1. Python 3.12.

**Method:** I read the installed package source and the official docs, then ran scripts against
the real compiled `triage_graph`. The runs used `tests/fakes.make_triage_deps` (a
`ScriptedAdapter` LLM, a canned Statuspage and in-memory history), so nothing touched the
network. The FastAPI checks used `httpx.ASGITransport`. The disconnect check used a real uvicorn
server on 127.0.0.1. Source paths below are relative to `.venv/lib/python3.12/site-packages/`.

## TL;DR for #90

- Use `triage_graph.astream(..., context=..., stream_mode=["tasks", "custom"], version="v2")`
  with **no checkpointer**. Every event arrives as `{"type", "ns", "data"}`. `tasks` gives a start
  event and a finish event for each node, so you get timing and step order. `custom` carries
  whatever nodes, or code called by nodes, write through `get_stream_writer()`. This works today
  with no checkpointer, but the official docs disagree (see §1.3).
- **Router return values (`"repair"`, `"compensate"`, …) are not in `updates`, `tasks` or
  `values`.** You can recover them without touching the routers. Look up the pair
  (previous node, next node) in the edge labels from `get_graph()`, which is unambiguous for this
  graph. If you want the exact return value, read the `on_chain_end` events of runs named
  `route_*` from a callback handler or from `astream_events`.
- `get_stream_writer()` works inside nodes and inside `LLMGateway`/`TelemetryProbe` code
  (including the probe's `asyncio.gather` children). **Outside a graph run it raises
  `RuntimeError`.** Both collaborators are also called outside runs, so a direct call there would
  break the never-raises promise. Guard it, or (better) pass a run-scoped recorder in through
  `context`.
- `context=` is per call and is passed through as the same object. A fresh
  `dataclasses.replace(deps, trace=Recorder())` per run works, and concurrent runs stay isolated.
- `fastapi.sse.EventSourceResponse` works on `POST`. It sends `: ping` every 15 s, adds
  `Cache-Control: no-cache` and `X-Accel-Buffering: no`, and cancels the generator **and the
  in-flight graph node** when the client disconnects. An exception after the first event cannot
  become a 500: the connection is simply cut. The generator must catch `Exception` and yield a
  terminal error event.

## 1. LangGraph stream modes

`StreamMode = Literal["values", "updates", "checkpoints", "tasks", "debug", "messages", "custom"]`
(`langgraph/types.py:131`). Pass a list to get several modes at once: v1 yields `(mode, data)`
tuples and v2 yields `StreamPart` dicts (`pregel/main.py` `astream` docstring, ~line 3172). The
official docs say: "every chunk is a `StreamPart` dict with a consistent shape" with keys
`type`/`ns`/`data`, and that this needs LangGraph >= 1.1
([streaming docs](https://docs.langchain.com/oss/python/langgraph/streaming)).

### 1.1 What each mode emits for this graph (observed)

| Mode | Per node | Observed on the happy path |
|---|---|---|
| `updates` | `{node_name: partial_update_dict}` after the node returns | 6 events: `analyze_query`, `collect_telemetry`, `synthesize_telemetry`, `generate_report`, `validate_report`, `finalize_report` |
| `values` | full state after each step, plus the input first | 7 events (the input, then 6 steps) |
| `tasks` | start: `{id, name, input, triggers}`; finish: `{id, name, error, interrupts, result}` | 12 events, a start/finish pair per node, emitted in real time around the node body |
| `debug` | `{step, timestamp, type: "task"\|"task_result"\|"checkpoint", payload}` | only `task`/`task_result` (no checkpointer, so no `checkpoint` events) |
| `checkpoints` | checkpoint snapshots | nothing: it is gated on a checkpointer (`pregel/_loop.py:683`, `if self._checkpointer_put_after_previous is not None`) |
| `messages` | `(token, metadata)` from LangChain chat models | nothing to expect: the LLM gateway calls Groq over raw HTTP, so no chat-model callbacks fire |
| `custom` | anything passed to the stream writer | see §2 |

Payload typing is defined in `langgraph/types.py:153` (`TaskPayload`) and
`langgraph/types.py:176` (`TaskResultPayload`). At runtime `triggers` is a **tuple**, not the
`list[str]` the TypedDict declares.

Facts the trace design depends on:

- `tasks` start `input` is the **whole state**: `messages` holds the full system prompt and LLM
  text, plus `IncidentTriageReport` objects and `ProviderFailure` dataclasses. `updates` and
  `values` carry the same kinds of objects. None of this can go to the wire as is. Project it to a
  small trace DTO.
- `tasks` timing is real. In the provider-failure scenario, `generate_report` started at 1.6 ms
  and finished at 517 ms (through retries and failover), so durations can come from the
  start/finish pair.
- The task `id` repeats between a node's start and finish events, but the self-repair loop gives
  each visit to `generate_report` a new id. Each visit is its own trace step.

### 1.2 Can router decisions be seen without changing routers?

**Not from `updates`/`values`/`tasks` directly.** A router is not a node. In every scenario,
static or conditional, `triggers` was just `('branch:to:<target>',)`. It names the destination
but neither the source nor the router key. Observed across the happy path, self-repair,
provider failure → fallback, and tripped breaker → compensate:

```
validate_report  triggers=('branch:to:validate_report',)
self_repair      triggers=('branch:to:self_repair',)       # router said "repair"
compensate_blindspots triggers=('branch:to:compensate_blindspots',)  # router said "compensate"
```

There are three ways to read the decision without editing the routers:

1. **Infer it from consecutive task names plus the `get_graph()` edge labels** (no extra
   machinery). This graph runs exactly one task per step, and within each router's path map every
   key points to a different node. That makes (source, target) → label unique, so
   `generate_report → finalize_report` means `"finalize"` and `validate_report → finalize_report`
   also means `"finalize"`. This holds only while every path map stays one key per node and steps
   stay serial. A test should pin it.
2. **A LangChain callback handler** passed as `config={"callbacks": [handler]}`. Routers run as
   child runnables named after the function. Observed with `on_chain_start` (which gets the
   `name=` kwarg) feeding `on_chain_end` (which gets the outputs):
   `route_after_telemetry -> synthesize`, `route_after_generation -> validate`,
   `route_after_validation -> repair`, … This gives the exact key.
3. **`astream_events(version="v2")`**: `on_chain_end` events named `route_*`, with
   `data.output` set to the key and `metadata.langgraph_node`/`langgraph_step` set to the source
   node and step. Observed `route_after_validation -> repair node=validate_report step=5`. This
   mode emits every runnable event, so it is heavy, and it means switching APIs away from
   `astream`.

### 1.3 Checkpointer requirement: the docs and the code disagree

The official streaming docs say the `checkpoints` **and `tasks`** modes require a checkpointer.
In 1.2.16 only `checkpoints` is gated (`pregel/_loop.py:683`). `tasks` is emitted
unconditionally: task starts at `pregel/_loop.py:728` (`self._emit("tasks", map_debug_tasks,
...)`) and results at `pregel/_loop.py:1638`. The graph compiles with `checkpointer=None`
(observed `triage_graph.checkpointer is None`), and `tasks`/`debug` events streamed normally.
**Risk:** a future LangGraph release could align the code with the docs. #90 should either rely
only on `updates` + `custom` (documented as checkpointer-free), or keep `tasks` and add a test
that fails if the events disappear.

## 2. `get_stream_writer()` and `runtime.stream_writer`

- `get_stream_writer()` is just `get_config()[CONF][CONFIG_KEY_RUNTIME].stream_writer`
  (`langgraph/config.py:195-196`), so it returns the same object as `runtime.stream_writer` on
  the `Runtime` a node receives (`langgraph/runtime.py:125`). It reads LangChain's contextvar.
  Since Python 3.11, asyncio tasks inherit contextvars, so it works in async code (docstring
  warning at `config.py:132`; the official docs say the same).
- In `astream`, the writer is set like this. When `"custom"` is among the modes, it does
  `aioloop.call_soon_threadsafe(stream.put_nowait, (ns, "custom", c))`, which is thread-safe and
  never blocks. Otherwise it is a no-op `def stream_writer(c): pass` (`pregel/main.py:3372-3394`).
  `ainvoke` therefore makes writer calls silent no-ops. Observed: with the adapter and probe
  patched to call `get_stream_writer()`, `ainvoke` completed normally.
- **From code called by nodes**, observed in the `custom` stream:
  - `{'from': 'ScriptedAdapter.call (under LLMGateway)', 'model': 'qwen/qwen3.8-27b'}`, written
    inside the adapter called by `LLMGateway.complete()` from `generate_report`.
  - `{'from': 'StatuspageSource.read (under TelemetryProbe gather)'}`, written inside a task
    spawned by `TelemetryProbe.collect`'s `asyncio.gather`. Child tasks copy the contextvar, so it
    works.
  - Custom events land between the node's `tasks` start and its `updates`, in real time.
- **No checkpointer needed.** All of the above ran on the ADR-007 graph (`checkpointer=None`).
  The writer depends on the stream queue, not on a saver.
- **Outside a graph run it raises:** `RuntimeError: Called get_config outside of a runnable
  context` (observed). `LLMGateway.complete()` and `TelemetryProbe.call_source()` are also called
  outside runs, for example `AppRuntime.probe_cluster_sentinel()` for `/chaos/*`. Calling
  `get_stream_writer()` there without a guard would break the "never raises" contract in
  `llm/gateway.py`. The writer closure itself also calls `get_config()` for the namespace, so a
  writer captured in a run and called later from a context-less callback raises too.

## 3. Per-run `context=`

- `astream`/`ainvoke` take `context` on every call (`pregel/main.py:3159`, "The static context
  to use for the run", added in 0.6.0). It is wrapped in a new `Runtime` per call
  (`pregel/main.py:~3403`). `_coerce_context` (`pregel/main.py:4527`) builds an object only from
  a **dict**. Any other object, such as the frozen `TriageDeps` dataclass or a replaced copy, is
  passed through as is. Nothing is copied, validated or cached between runs.
- Today `AppRuntime.run_graph` passes the shared `self.deps` (`runtime.py:115`). A run-scoped
  recorder can be added as a new `TriageDeps` field and built per run with
  `dataclasses.replace(self.deps, trace=Recorder())`. The other collaborators stay shared.
- Observed: two concurrent `astream` calls with distinct context objects each received only
  their own custom events (2 each). Isolation comes from the per-call stream queue and contextvar
  propagation.
- A recorder in `context` is the option that also works **outside** a run and under `ainvoke`:
  the code writes to the object it was handed, with no contextvar lookup. The stream writer is
  only the transport for live delivery.

## 4. `fastapi.sse` (FastAPI 0.161.1)

Official guide: [Server-Sent Events](https://fastapi.tiangolo.com/tutorial/server-sent-events/)
("Added in FastAPI 0.135.0").

### API

- `EventSourceResponse(StreamingResponse)` is a marker class with
  `media_type = "text/event-stream"` (`fastapi/sse.py:20`). Use it as
  `response_class=EventSourceResponse` on a **generator** path operation. The route detects it in
  `fastapi/routing.py:1128`. The docstring says it "Works with **any HTTP method** (`GET`,
  `POST`, etc.)".
- `ServerSentEvent(BaseModel)` (`fastapi/sse.py:52`) has these fields:
  - `data`: always JSON-encoded, so a plain string arrives quoted. A model goes through
    `model_dump_json()`, anything else through `json.dumps(jsonable_encoder(...))`
    (`routing.py:559-583`).
  - `raw_data`: sent as is, and mutually exclusive with `data`.
  - `event`: must be a single line.
  - `id`: single line, no NUL.
  - `retry`: in ms, at least 0.
  - `comment`.
- A plain (non-`ServerSentEvent`) yield goes out as a bare `data:` line. When the return
  annotation is `AsyncIterable[Model]`, each item is validated against `Model`; a violation
  raises `ResponseValidationError` mid-stream. `ServerSentEvent` items skip that validation
  (`routing.py:1142-1151`).

### Keepalive

The constant is `_PING_INTERVAL = 15.0` and the payload is `KEEPALIVE_COMMENT = b": ping\n\n"`
(`sse.py`, end of file). A producer task pulls from the generator into a memory stream with a
buffer of 1. A second task waits up to `_PING_INTERVAL` for the next item and, on timeout,
inserts the ping (`routing.py:598-646`). The `fail_after` therefore never wraps the generator's
`__anext__`, and a slow LLM call is not cancelled by a ping. `routing.py` imports the name with
`from fastapi.sse import _PING_INTERVAL`, so **tests must monkeypatch
`fastapi.routing._PING_INTERVAL`**, not `fastapi.sse._PING_INTERVAL`. Observed with 0.1 s:
`data: "first"\n\n: ping\n\n: ping\n\n: ping\n\ndata: "second"…`. The interval cannot be set
per route.

### Headers

FastAPI adds `Cache-Control: no-cache` and `X-Accel-Buffering: no`, and keeps any headers a
`Response` dependency set (`routing.py:680-683`). Observed: `content-type: text/event-stream;
charset=utf-8`.

### Client disconnect

uvicorn's HTTP protocols advertise ASGI `spec_version` "2.3" (`uvicorn/protocols/http/h11_impl.py:210`,
`httptools_impl.py:228`). Starlette's `StreamingResponse` therefore uses the
`listen_for_disconnect` path: on `http.disconnect` it cancels the task group streaming the body
(`starlette/responses.py:270-285`). FastAPI's SSE producer task group sits on the request
exit stack and is cancelled when that stack unwinds (`routing.py:649-658`). Observed with real
uvicorn: the client read up to `synthesize_telemetry` and closed. The log then showed
`LLM call CANCELLED` (inside `generate_report`), `generator got CancelledError`, and
`generator finally`. **A disconnect cancels the graph run.** Consequences:

- `TriageGateway` stores results to the cache after the graph returns, so nothing is stored for
  an abandoned stream. Run slots must be released in `finally`/`async with`.
- Because the producer buffers 1 item, the generator runs at most about 2 events ahead of the
  client.

### Errors mid-stream

Headers (status 200) have already gone out by the time the generator runs. Observed: raising
after the first yield surfaced as an `ExceptionGroup` (from the producer's task group) and cut
the stream. No status change or JSON error body is possible, and app exception handlers do not
apply. To keep "never 500", the generator must wrap the run in `except Exception` and yield a
terminal `event: error` (or a degraded `report`) event. It must **not** catch
`BaseException`/`CancelledError`, because that would swallow the disconnect cancellation.

### POST vs GET

Server-side, both behave the same: the body is parsed by dependencies before streaming starts.
Observed: `POST /trace` with a JSON body streamed fine. The constraint is on the client side. The
browser `EventSource` constructor takes only a URL and always issues a GET, with no body and no
custom headers
([WHATWG HTML §9.2](https://html.spec.whatwg.org/multipage/server-sent-events.html#the-eventsource-interface)).
That leaves a POST trace two options:

- read it with `fetch()` plus a `ReadableStream` SSE parser, losing automatic reconnect and
  `Last-Event-ID`, or
- split it into `POST` (start the run, return a run id) followed by `GET` (stream it).

The splitting option needs run state held across requests, which ADR-007's "each run is
self-contained" does not provide today. The FastAPI guide's `Last-Event-ID` resume pattern
(`Header()` param) also assumes the server can replay. A self-contained run cannot replay, so on
reconnect it would re-run the triage.

### OpenAPI shape

From `fastapi/openapi/utils.py:428-447`, observed:

- `AsyncIterable[ServerSentEvent]` → `responses.200.content["text/event-stream"].itemSchema` =
  `{type: object, properties: {data: {type: string}, event: {type: string}, id: {type: string},
  retry: {type: integer, minimum: 0}}}`. The payload is untyped.
- `AsyncIterable[Model]` → the same, plus `required: ["data"]` and
  `data: {type: string, contentMediaType: "application/json", contentSchema: {$ref:
  "#/components/schemas/Model"}}`. `itemSchema` follows OpenAPI 3.2 §4.14.4 (comment at
  `sse.py:7`).
- The `event` names are not documented in either case. Typed docs need `AsyncIterable[Model]`,
  which costs control over `event:`/`id:`. Otherwise describe the event types in prose.

## 5. `get_graph()` output (exact)

`triage_graph.get_graph()` gives the following.

**Nodes** (`list(g.nodes)`):

```
['__start__', 'analyze_query', 'collect_telemetry', 'compensate_blindspots', 'degraded_fallback',
 'synthesize_telemetry', 'generate_report', 'validate_report', 'self_repair', 'refine_reflection',
 'fallback_report', 'finalize_report', '__end__']
```

The finalize node's id is **`finalize_report`**, not `finalize`. `finalize` is only a router
key. `__end__` has `data=None`. Every other node is a `RunnableCallable`.

**Edges** are `Edge(source, target, data, conditional)`. For conditional edges, `data` is the
router key and `conditional=True`. Static edges have `data=None`.

```
__start__ -> analyze_query                       static
analyze_query -> collect_telemetry               static
collect_telemetry -> compensate_blindspots       'compensate'  conditional
collect_telemetry -> synthesize_telemetry        'synthesize'  conditional
compensate_blindspots -> degraded_fallback       static
degraded_fallback -> synthesize_telemetry        static
fallback_report -> finalize_report               static
generate_report -> fallback_report               'fallback'    conditional
generate_report -> finalize_report               'finalize'    conditional
generate_report -> validate_report               'validate'    conditional
refine_reflection -> generate_report             static
self_repair -> generate_report                   static
synthesize_telemetry -> generate_report          static
validate_report -> fallback_report               'fallback'    conditional
validate_report -> finalize_report               'finalize'    conditional
validate_report -> refine_reflection             'refine'      conditional
validate_report -> self_repair                   'repair'      conditional
finalize_report -> __end__                       static
```

`draw_mermaid()` renders conditional edges as `src -. &nbsp;label&nbsp; .-> dst;` and static
edges as `src --> dst;`. `__start__` gets the class `first` and `__end__` the class `last`.
Everything above comes from the router path maps in `graph/builder.py`. A router without a path
map would produce edges to every node, with no labels.

## Sources

- Installed source:
  - `langgraph/types.py`
  - `langgraph/config.py`
  - `langgraph/runtime.py`
  - `langgraph/pregel/main.py`
  - `langgraph/pregel/_loop.py`
  - `fastapi/sse.py`
  - `fastapi/routing.py`
  - `fastapi/openapi/utils.py`
  - `starlette/responses.py`
  - `uvicorn/protocols/http/{h11,httptools}_impl.py`
- Repo:
  - `src/resilient_triage/graph/builder.py`
  - `graph/deps.py`
  - `runtime.py`
  - `telemetry/probe.py`
  - `llm/gateway.py`
  - `docs/decisions/0007-no-checkpointing-on-the-request-path.md`
- Official docs:
  - https://docs.langchain.com/oss/python/langgraph/streaming
  - https://fastapi.tiangolo.com/tutorial/server-sent-events/
  - https://html.spec.whatwg.org/multipage/server-sent-events.html
