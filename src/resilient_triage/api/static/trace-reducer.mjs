// @ts-check
// The trace-to-view reducer (ADR 0008, #118): the page's one logic module. Everything the page
// shows about a run is derived here from the run's trace events; components only draw what
// these functions return (CODING_STANDARDS.md, Frontend).

/**
 * One trace event as it arrives on the wire (trace/events.py); `kind` discriminates.
 * @typedef {{ kind: string, seq: number, t_ms: number, [field: string]: any }} TraceEvent
 */

/**
 * The JS port of trace/summary.py's TraceSummary, field for field.
 * @typedef {{
 *   scenario_id: string | null,
 *   outcome: "cache_hit" | "analyzed" | "shed" | null,
 *   nodes: { node: string, duration_ms: number | null, status: string }[],
 *   routes: string[],
 *   model_calls: { model: string, role: string, outcome: string, duration_ms: number }[],
 *   failovers: number,
 *   telemetry: { source: string, outcome: string, attempts: number }[],
 *   repair_rounds: number,
 *   refine_rounds: number,
 *   reconciliation: Record<string, any> | null,
 *   degradation: string | null,
 *   deadline_exceeded: boolean,
 *   timings: {
 *     total_ms: number,
 *     cache_lookup_ms: number | null,
 *     slot_wait_ms: number | null,
 *     run_ms: number | null,
 *     model_ms: number,
 *   },
 * }} TraceSummary
 */

// ---------------------------------------------------------------------------------------------
// Trace view: the running fold of one run's events. reduceTrace() takes one event at a time,
// so the page redraws as each arrives; foldTrace() folds a whole list.

/**
 * One visit to a graph node; `endMs` is null while the node runs.
 * @typedef {{ node: string, startMs: number, endMs: number | null, status: string,
 *             confidence: number | null }} NodeVisitView
 */

/**
 * @typedef {{
 *   events: TraceEvent[],
 *   scenarioId: string | null,
 *   scripted: boolean,
 *   visits: NodeVisitView[],
 *   activeNode: string | null,
 *   walked: string[],
 *   finished: boolean,
 * }} TraceView
 * `scripted`: a scenario run, whose model replies are scripted (ADR 0009). `walked`: graph
 * edges the run has taken, as edgeKey()s, each once, in the order first taken.
 */

const START = "__start__";
const END = "__end__";

/**
 * The key naming one graph edge; fallback and finalize each sit on two routers, so an edge
 * is its (source, target) pair, never its route alone.
 * @param {string} source
 * @param {string} target
 */
export function edgeKey(source, target) {
  return `${source}>${target}`;
}

/** @returns {TraceView} The view before any event. */
export function initialTrace() {
  return {
    events: [],
    scenarioId: null,
    scripted: false,
    visits: [],
    activeNode: null,
    walked: [],
    finished: false,
  };
}

/**
 * @param {string[]} walked
 * @param {string} key
 */
function walk(walked, key) {
  return walked.includes(key) ? walked : [...walked, key];
}

/**
 * Fold one event into the view; pure, returns a new view.
 * @param {TraceView} trace
 * @param {TraceEvent} event
 * @returns {TraceView}
 */
export function reduceTrace(trace, event) {
  const next = { ...trace, events: [...trace.events, event] };
  switch (event.kind) {
    case "trace_started":
      next.scenarioId = event.scenario_id;
      next.scripted = event.scenario_id !== null;
      break;
    case "node_started": {
      const previous = trace.visits.length ? trace.visits[trace.visits.length - 1].node : START;
      next.walked = walk(trace.walked, edgeKey(previous, event.node));
      next.visits = [
        ...trace.visits,
        { node: event.node, startMs: event.t_ms, endMs: null, status: "running", confidence: null },
      ];
      next.activeNode = event.node;
      break;
    }
    case "node_finished": {
      const index = trace.visits.findLastIndex((v) => v.node === event.node && v.endMs === null);
      if (index !== -1) {
        next.visits = trace.visits.with(index, {
          ...trace.visits[index],
          endMs: event.t_ms,
          status: event.status,
          confidence: event.confidence ?? null,
        });
      }
      if (trace.activeNode === event.node) next.activeNode = null;
      break;
    }
    case "trace_finished":
      next.finished = true;
      next.activeNode = null;
      if (trace.visits.length) {
        next.walked = walk(trace.walked, edgeKey(trace.visits[trace.visits.length - 1].node, END));
      }
      break;
  }
  return next;
}

/**
 * Fold a whole event list.
 * @param {TraceEvent[]} events
 */
export function foldTrace(events) {
  return events.reduce(reduceTrace, initialTrace());
}

// ---------------------------------------------------------------------------------------------
// Graph view: graph-layout.json plus a trace, as the shapes graph.mjs draws.

/**
 * graph-layout.json (tests/test_graph_layout.py keeps it equal to the compiled graph).
 * @typedef {{
 *   view_box: [number, number, number, number],
 *   node_height: number,
 *   font: { node_px: number, label_px: number },
 *   nodes: Record<string, { x: number, y: number, width?: number, r?: number }>,
 *   edges: { source: string, target: string, points: [number, number][], route?: string,
 *            label?: [number, number] }[],
 * }} GraphLayout
 */

/**
 * @typedef {{ name: string, terminal: boolean, x: number, y: number, width: number,
 *             height: number, r: number, visits: number,
 *             state: "idle" | "visited" | "active" }} GraphNodeView
 * @typedef {{ key: string, source: string, target: string, route: string | null, d: string,
 *             arrow: string, label: { x: number, y: number } | null,
 *             taken: boolean }} GraphEdgeView
 * @typedef {{ viewBox: string, nodePx: number, labelPx: number, nodes: GraphNodeView[],
 *             edges: GraphEdgeView[], visited: number, total: number }} GraphView
 */

const ARROW_LENGTH = 6;
const ARROW_HALF_WIDTH = 3.5;

/**
 * The arrowhead polygon at a polyline's end, pointing along its last segment.
 * @param {[number, number][]} points
 */
function arrowhead(points) {
  const [tx, ty] = points[points.length - 1];
  const [fx, fy] = points[points.length - 2];
  const length = Math.hypot(tx - fx, ty - fy) || 1;
  const [dx, dy] = [(tx - fx) / length, (ty - fy) / length];
  const [bx, by] = [tx - dx * ARROW_LENGTH, ty - dy * ARROW_LENGTH];
  const corner = (/** @type {number} */ side) =>
    `${bx - dy * ARROW_HALF_WIDTH * side},${by + dx * ARROW_HALF_WIDTH * side}`;
  return `${tx},${ty} ${corner(1)} ${corner(-1)}`;
}

/**
 * Where the run has been on the graph: each node's visits and state, each edge taken or not.
 * @param {GraphLayout} layout
 * @param {TraceView} trace
 * @returns {GraphView}
 */
export function graphView(layout, trace) {
  /** @type {Record<string, number>} */
  const visits = {};
  for (const visit of trace.visits) visits[visit.node] = (visits[visit.node] ?? 0) + 1;
  if (trace.visits.length) visits[START] = 1;
  if (trace.walked.some((key) => key.endsWith(`>${END}`))) visits[END] = 1;

  const nodes = Object.entries(layout.nodes).map(([name, box]) => {
    const count = visits[name] ?? 0;
    /** @type {GraphNodeView["state"]} */
    const state = trace.activeNode === name ? "active" : count ? "visited" : "idle";
    return {
      name,
      terminal: box.r !== undefined,
      x: box.x,
      y: box.y,
      width: box.width ?? 0,
      height: layout.node_height,
      r: box.r ?? 0,
      visits: count,
      state,
    };
  });
  const edges = layout.edges.map((edge) => {
    const key = edgeKey(edge.source, edge.target);
    return {
      key,
      source: edge.source,
      target: edge.target,
      route: edge.route ?? null,
      d: edge.points.map(([x, y], i) => `${i ? "L" : "M"}${x} ${y}`).join(" "),
      arrow: arrowhead(edge.points),
      label: edge.label ? { x: edge.label[0], y: edge.label[1] } : null,
      taken: trace.walked.includes(key),
    };
  });
  const graphNodes = nodes.filter((node) => !node.terminal);
  return {
    viewBox: layout.view_box.join(" "),
    nodePx: layout.font.node_px,
    labelPx: layout.font.label_px,
    nodes,
    // Taken edges last, so they draw over the idle edges they share a corridor with.
    edges: edges.filter((edge) => !edge.taken).concat(edges.filter((edge) => edge.taken)),
    visited: graphNodes.filter((node) => node.visits).length,
    total: graphNodes.length,
  };
}

// ---------------------------------------------------------------------------------------------
// Summary: the JS port of trace/summary.py, checked against it by tests/browser/test_reducer.py.

// Node names as registered in graph/builder.py; keep them in step with trace/summary.py.
const SELF_REPAIR_NODE = "self_repair";
const REFINE_NODE = "refine_reflection";

/**
 * Round to microseconds, as the recorder stamps t_ms (summary.py `_round_ms`).
 * @param {number | null} ms
 */
function roundMs(ms) {
  return ms === null ? null : Math.round(ms * 1000) / 1000;
}

/**
 * Condense an event list into its trace summary, exactly as trace/summary.py `summarize()`.
 * @param {TraceEvent[]} events
 * @returns {TraceSummary}
 */
export function summarize(events) {
  let originMs = 0;
  /** @type {string | null} */
  let scenarioId = null;
  /** @type {TraceSummary["outcome"]} */
  let outcome = null;
  /** @type {TraceSummary["outcome"]} */
  let finishedOutcome = null;
  /** @type {TraceSummary["nodes"]} */
  const nodes = [];
  /** @type {string[]} */
  const routes = [];
  /** @type {TraceSummary["model_calls"]} */
  const modelCalls = [];
  /** @type {TraceSummary["telemetry"]} */
  const telemetry = [];
  /** @type {TraceSummary["reconciliation"]} */
  let reconciliation = null;
  /** @type {string | null} */
  let degradation = null;
  let deadlineExceeded = false;
  /** @type {number | null} */
  let cacheLookupMs = null;
  /** @type {number | null} */
  let slotWaitMs = null;
  /** @type {number | null} */
  let runStartedMs = null;
  /** @type {number | null} */
  let runFinishedMs = null;

  for (const event of events) {
    switch (event.kind) {
      case "trace_started":
        originMs = event.t_ms;
        scenarioId = event.scenario_id;
        break;
      case "cache_lookup":
        cacheLookupMs = event.duration_ms;
        if (event.hit) outcome = "cache_hit";
        break;
      case "run_slot":
        slotWaitMs = event.waited_ms;
        outcome = event.acquired ? "analyzed" : "shed";
        break;
      case "node_started":
        nodes.push({ node: event.node, duration_ms: null, status: "ok" });
        if (runStartedMs === null) runStartedMs = event.t_ms;
        break;
      case "node_finished": {
        const open = findLastOpenVisit(nodes, event.node);
        if (open) Object.assign(open, { duration_ms: event.duration_ms, status: event.status });
        runFinishedMs = event.t_ms;
        break;
      }
      case "route_decided":
        routes.push(event.route);
        break;
      case "model_call":
        modelCalls.push({
          model: event.model,
          role: event.role,
          outcome: event.outcome,
          duration_ms: event.duration_ms,
        });
        break;
      case "telemetry_read":
        if (event.phase === null) {
          telemetry.push({ source: event.source, outcome: event.outcome, attempts: event.attempts });
        }
        break;
      case "reconciled":
        reconciliation = event.changes;
        degradation = event.changes.degradation_after;
        break;
      case "deadline_exceeded":
        deadlineExceeded = true;
        break;
      case "trace_finished":
        finishedOutcome = event.outcome;
        if (event.response) degradation = event.response.report.degradation_status;
        break;
    }
  }

  const lastMs = events.length ? events[events.length - 1].t_ms : originMs;
  const runMs =
    runStartedMs !== null && runFinishedMs !== null ? runFinishedMs - runStartedMs : null;
  // Summed in event order, as Python's sum(), so the float result is the same.
  let modelMs = 0;
  for (const call of modelCalls) modelMs += call.duration_ms;
  return {
    scenario_id: scenarioId,
    outcome: finishedOutcome || outcome,
    nodes,
    routes,
    model_calls: modelCalls,
    failovers: modelCalls.filter((call) => call.role === "fallback").length,
    telemetry,
    repair_rounds: nodes.filter((visit) => visit.node === SELF_REPAIR_NODE).length,
    refine_rounds: nodes.filter((visit) => visit.node === REFINE_NODE).length,
    reconciliation,
    degradation,
    deadline_exceeded: deadlineExceeded,
    timings: {
      total_ms: /** @type {number} */ (roundMs(Math.max(lastMs - originMs, 0))),
      cache_lookup_ms: roundMs(cacheLookupMs),
      slot_wait_ms: roundMs(slotWaitMs),
      run_ms: roundMs(runMs),
      model_ms: /** @type {number} */ (roundMs(modelMs)),
    },
  };
}

/**
 * The latest visit to a node that hasn't finished (runs are serial, so it is the last one).
 * @param {TraceSummary["nodes"]} nodes
 * @param {string} node
 */
function findLastOpenVisit(nodes, node) {
  for (let index = nodes.length - 1; index >= 0; index--) {
    const visit = nodes[index];
    if (visit.node === node && visit.duration_ms === null) return visit;
  }
  return null;
}

// Display names of the Telemetry sources, by the id the trace carries (telemetry/*.py
// `display_name`). An unknown id shows as itself.
const SOURCE_NAMES = /** @type {Record<string, string>} */ ({
  statuspage: "Statuspage",
  chaos: "Cluster Sentinel",
});

/**
 * A source's display name.
 * @param {string} source
 */
export function sourceName(source) {
  return SOURCE_NAMES[source] ?? source;
}

/**
 * A duration for people: "under 1 ms", "917 ms", "1.8 s".
 * @param {number} ms
 */
export function formatMs(ms) {
  if (ms < 0.5) return "under 1 ms";
  if (ms < 999.5) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/**
 * "1 thing" or "3 things".
 * @param {number} count
 * @param {string} one
 * @param {string} many
 */
function plural(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * One line saying how a run ended: its triage outcome, then what bent the run on the way.
 * Built from the summary alone, never from the scenario (ADR 0008: no free text on the wire).
 * @param {TraceSummary} summary
 */
export function summaryLine(summary) {
  const total = formatMs(summary.timings.total_ms);
  if (summary.outcome === "cache_hit") return `Answered from cache in ${total}`;
  if (summary.outcome === "shed") return `Shed after ${total}, before the graph ran`;
  if (summary.outcome === null) return `No answer yet, ${total} in`;

  const head = summary.degradation === "SEVERELY_DEGRADED" ? "Fallback report" : "Report";
  /** @type {string[]} */
  const parts = [`${head} in ${total}`];
  if (summary.deadline_exceeded) parts.push("run deadline hit");
  /** @type {Set<string>} */
  const seen = new Set();
  for (const read of summary.telemetry) {
    if (read.outcome === "read") continue;
    const part = `${sourceName(read.source)} ${read.outcome === "tripped" ? "breaker open" : "unavailable"}`;
    if (!seen.has(part)) parts.push(part);
    seen.add(part);
  }
  if (summary.repair_rounds) parts.push(plural(summary.repair_rounds, "self-repair round", "self-repair rounds"));
  if (summary.refine_rounds) parts.push(plural(summary.refine_rounds, "refine round", "refine rounds"));
  if (summary.failovers) parts.push(plural(summary.failovers, "model failover", "model failovers"));
  const lastCall = summary.model_calls[summary.model_calls.length - 1];
  if (lastCall && lastCall.outcome !== "completion") parts.push("no model answered");
  const held = summary.reconciliation?.approval_raised?.length ?? 0;
  if (held) parts.push(`${plural(held, "action", "actions")} held for approval`);
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------------------------
// Run controller: plays one stream source at a time into a RunView, with the stream watchdog
// (#101): 30 s with no event or ping, or a close before trace_finished, ends the run "lost"
// with its partial trace kept. Never an endless spinner.

/** How long a stream may go without an event or a ping before the run counts as lost. */
export const STREAM_WATCHDOG_MS = 30_000;

/**
 * idle: nothing played yet. streaming: events arriving. finished: trace_finished arrived
 * (a cache hit, an analyzed run or a shed alike). lost: the stream went silent or closed
 * early; the partial trace stays. error: the stream was refused or never connected.
 * @typedef {"idle" | "streaming" | "finished" | "lost" | "error"} RunStatus
 * @typedef {{ status: RunStatus, trace: TraceView, error: string | null }} RunView
 * @typedef {import("./sse.mjs").StreamSource} StreamSource
 */

/**
 * What the stage says about a run: its one line, whether to dim the partial trace, and the
 * action its button offers (Replay after a lost stream, Retry after a refusal).
 * @typedef {{ status: RunStatus, line: string | null, dimmed: boolean,
 *             action: "Replay" | "Retry" | null }} RunHeadline
 * @param {RunView} run
 * @returns {RunHeadline}
 */
export function runHeadline(run) {
  const { status, trace } = run;
  const lastMs = trace.events.length ? trace.events[trace.events.length - 1].t_ms : 0;
  switch (status) {
    case "idle":
      return { status, line: null, dimmed: false, action: null };
    case "streaming":
      return { status, line: `Running, ${formatMs(lastMs)} in`, dimmed: false, action: null };
    case "finished":
      return { status, line: summaryLine(summarize(trace.events)), dimmed: false, action: null };
    case "lost":
      return { status, line: "Connection lost", dimmed: true, action: "Replay" };
    case "error":
      return { status, line: run.error, dimmed: false, action: "Retry" };
  }
}

/** @returns {RunView} */
export function idleRun() {
  return { status: "idle", trace: initialTrace(), error: null };
}

const UNREACHABLE = "Couldn't reach the server. Check your connection and try again.";

/**
 * Plays stream sources into RunViews, reporting each new view to `onChange`. One run at a
 * time: play() supersedes the run before it and resolves with the run's last view.
 * @param {{ onChange: (view: RunView) => void, watchdogMs?: number }} options
 */
export function createRunController({ onChange, watchdogMs = STREAM_WATCHDOG_MS }) {
  let view = idleRun();
  let runId = 0;
  /** @type {AbortController | null} */
  let current = null;
  /** @type {StreamSource | null} */
  let lastSource = null;

  /** @param {RunView} next */
  const set = (next) => {
    view = next;
    onChange(view);
  };

  /**
   * Play a source from the start; resolves when the run ends, however it ends.
   * @param {StreamSource} source
   * @returns {Promise<RunView>}
   */
  async function play(source) {
    current?.abort();
    const id = ++runId;
    const abort = new AbortController();
    current = abort;
    lastSource = source;
    const live = () => id === runId;
    /** @type {ReturnType<typeof setTimeout> | undefined} */
    let timer;
    /**
     * End this run in a terminal state; a no-op once it has ended or been superseded.
     * @param {RunStatus} status
     * @param {string | null} [error]
     */
    const end = (status, error = null) => {
      clearTimeout(timer);
      if (!live()) return;
      runId++; // anything the old stream still yields is ignored from here
      current = null;
      // A finished stream closes itself once trace_finished is sent; only cut a stalled one.
      if (status !== "finished") abort.abort();
      set({ ...view, status, error });
    };
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(() => end("lost"), watchdogMs);
    };

    set({ status: "streaming", trace: initialTrace(), error: null });
    arm();
    try {
      for await (const message of source(abort.signal)) {
        if (!live()) break;
        arm();
        if (message.kind !== "event") continue;
        const trace = reduceTrace(view.trace, message.event);
        set({ ...view, trace });
        if (trace.finished) {
          end("finished");
          break;
        }
      }
      end("lost"); // closed before trace_finished
    } catch (error) {
      const refused = error instanceof Error && error.name === "StreamHttpError";
      if (refused) end("error", error.message);
      else if (view.trace.events.length) end("lost");
      else end("error", UNREACHABLE);
    }
    return view;
  }

  return {
    play,
    /** Play the last source again from the start (the Replay and Retry buttons). */
    replay() {
      return lastSource ? play(lastSource) : Promise.resolve(view);
    },
    /** Stop the current run, if any, and go back to idle. */
    stop() {
      runId++;
      current?.abort();
      current = null;
      set(idleRun());
    },
    /** The current view. */
    get view() {
      return view;
    },
  };
}
