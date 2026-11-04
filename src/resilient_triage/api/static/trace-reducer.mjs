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
  // __end__ lights only through a graph edge: a run cut short by its deadline never gets there.
  const ended = layout.edges.some(
    (edge) => edge.target === END && trace.walked.includes(edgeKey(edge.source, END)),
  );
  if (ended) visits[END] = 1;

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
// Waterfall view: every step of the run as a bar on one clock. t_ms counts from trace_started;
// setup runs before it (its events are stamped 0), so setup draws left of the zero line, its
// reads laid end to end by their real durations.

/**
 * @typedef {"setup" | "cache" | "slot" | "node" | "model" | "telemetry" | "store" | "deadline"}
 *   RowKind
 * @typedef {"ok" | "warn" | "bad" | "running" | "setup"} RowTone
 * @typedef {{ key: string, kind: RowKind, name: string, detail: string, tag: string | null,
 *             depth: 0 | 1, startMs: number, durationMs: number, tone: RowTone,
 *             leftPct: number, widthPct: number }} WaterfallRow
 * @typedef {{ rows: WaterfallRow[], setupMs: number, runMs: number, zeroPct: number,
 *             endLabel: string }} WaterfallView
 */

// Setup with no measured duration (seeding the cache, holding slots) still gets this share of
// the axis, so it shows before t = 0.
const MIN_SETUP_SHARE = 0.06;

const SETUP_STEPS = /** @type {Record<string, (e: TraceEvent) => string>} */ ({
  seed_cache: (e) => `Seed the cache with ${plural(e.count, "answer", "answers")}`,
  hold_run_slots: (e) => `Hold ${plural(e.count, "run slot", "run slots")}`,
  fail_source: (e) => `Fail ${sourceName(e.source)} ${plural(e.count, "time", "times")}`,
  set_run_deadline: (e) => `Set a ${e.deadline_s} s run deadline`,
});

const SHED_REASONS = /** @type {Record<string, string>} */ ({
  capacity: "every run slot busy",
  budget: "free-play budget spent",
});

/**
 * A wire literal for people: "rate_limited" reads "rate limited".
 * @param {string} value
 */
function words(value) {
  return value.replaceAll("_", " ");
}

/**
 * A telemetry read's outcome, its attempts and any breaker change.
 * @param {TraceEvent} e
 */
function readDetail(e) {
  const parts = [words(e.outcome), plural(e.attempts, "attempt", "attempts")];
  if (e.breaker_before !== e.breaker_after) {
    parts.push(`breaker ${e.breaker_before} to ${e.breaker_after}`);
  }
  return parts.join(" · ");
}

/** @param {string} outcome */
function readTone(outcome) {
  return outcome === "read" ? "ok" : outcome === "tripped" ? "bad" : "warn";
}

/**
 * The run as waterfall rows, each placed on one axis from the start of setup to the run's end.
 * @param {TraceView} trace
 * @returns {WaterfallView}
 */
export function waterfallView(trace) {
  const lastMs = trace.events.length ? Math.max(0, trace.events[trace.events.length - 1].t_ms) : 0;
  /** @typedef {Omit<WaterfallRow, "leftPct" | "widthPct">} PlacedRow */
  /** @type {PlacedRow[]} */
  const rows = [];
  let setupCursor = 0; // where the next setup read starts: setup reads lie end to end from 0
  let stepStart = 0; // where the current setup step's first read started
  let open = 0; // node visits started and not yet finished: steps inside one are indented
  /** @type {number[]} row index of each node visit, in visit order */
  const visitRows = [];

  /**
   * Add a row; `startMs` and `durationMs` are on the run's clock unless kind is "setup".
   * @param {TraceEvent} e
   * @param {Pick<PlacedRow, "kind" | "name" | "detail" | "startMs" | "durationMs" | "tone">
   *         & Partial<Pick<PlacedRow, "tag">>} fields
   */
  const add = (e, fields) => {
    rows.push({ key: String(e.seq), tag: null, depth: open > 0 ? 1 : 0, ...fields });
  };
  /**
   * A step stamped when it ended (t_ms) that took durationMs: where it started, and how long.
   * @param {TraceEvent} e
   * @param {number} durationMs
   */
  const endedAt = (e, durationMs) => ({ startMs: Math.max(0, e.t_ms - durationMs), durationMs });

  for (const e of trace.events) {
    switch (e.kind) {
      case "scenario_setup": {
        // A step is sent once done, after the setup reads it made: its bar spans them.
        const name = SETUP_STEPS[e.step]?.(e) ?? words(e.step);
        const span = { startMs: stepStart, durationMs: setupCursor - stepStart };
        add(e, { kind: "setup", name, detail: "setup", tone: "setup", ...span });
        stepStart = setupCursor;
        break;
      }
      case "telemetry_read": {
        const name = `${sourceName(e.source)} read`;
        if (e.phase === "setup") {
          const span = { startMs: setupCursor, durationMs: e.duration_ms };
          add(e, { kind: "setup", name, detail: readDetail(e), tone: "setup", ...span });
          setupCursor += e.duration_ms;
        } else {
          const tone = readTone(e.outcome);
          const span = endedAt(e, e.duration_ms);
          add(e, { kind: "telemetry", name, detail: readDetail(e), tone, ...span });
        }
        break;
      }
      case "cache_lookup": {
        const detail = e.hit ? `hit · similarity ${e.similarity.toFixed(2)}` : "miss";
        const span = endedAt(e, e.duration_ms);
        add(e, { kind: "cache", name: "cache lookup", detail, tone: "ok", ...span });
        break;
      }
      case "run_slot": {
        const detail = e.acquired
          ? `acquired · waited ${formatMs(e.waited_ms)}`
          : `shed · ${SHED_REASONS[e.reason] ?? "no slot free"}`;
        const tone = e.acquired ? "ok" : "warn";
        add(e, { kind: "slot", name: "run slot", detail, tone, ...endedAt(e, e.waited_ms) });
        break;
      }
      case "node_started": {
        // Open until node_finished: its bar runs to the latest event.
        const span = { startMs: e.t_ms, durationMs: Math.max(0, lastMs - e.t_ms) };
        visitRows.push(rows.length);
        add(e, { kind: "node", name: e.node, detail: "running", tone: "running", ...span });
        open++;
        break;
      }
      case "node_finished":
        open = Math.max(0, open - 1);
        break;
      case "model_call": {
        const completed = e.outcome === "completion";
        add(e, {
          kind: "model",
          name: e.model,
          detail: `${e.role} · ${completed ? "reply" : words(e.outcome)}`,
          tag: trace.scripted ? "scripted reply" : null,
          tone: completed ? "ok" : "warn",
          ...endedAt(e, e.duration_ms),
        });
        break;
      }
      case "cache_store": {
        const tone = e.outcome === "stored" ? "ok" : "warn";
        const span = endedAt(e, e.duration_ms);
        add(e, { kind: "store", name: "cache store", detail: words(e.outcome), tone, ...span });
        break;
      }
      case "deadline_exceeded": {
        const detail = `hit at ${e.deadline_s} s`;
        const span = { startMs: e.t_ms, durationMs: 0 };
        add(e, { kind: "deadline", name: "run deadline", detail, tone: "bad", ...span });
        break;
      }
    }
  }

  // Close each finished visit's row with its real duration, status and confidence.
  trace.visits.forEach((visit, index) => {
    const rowIndex = visitRows[index];
    if (rowIndex === undefined || visit.endMs === null) return;
    const durationMs = visit.endMs - visit.startMs;
    const parts = [formatMs(durationMs)];
    if (visit.status !== "ok") parts.push(visit.status);
    if (visit.confidence !== null) parts.push(`confidence ${visit.confidence.toFixed(2)}`);
    const tone = visit.status === "ok" ? "ok" : visit.status === "cancelled" ? "warn" : "bad";
    rows[rowIndex] = { ...rows[rowIndex], detail: parts.join(" · "), durationMs, tone };
  });

  const runEnds = rows.filter((r) => r.kind !== "setup").map((r) => r.startMs + r.durationMs);
  const runMs = Math.max(lastMs, ...runEnds);
  const hasSetup = rows.some((r) => r.kind === "setup");
  const setupMs = hasSetup ? Math.max(setupCursor, MIN_SETUP_SHARE * runMs) : 0;
  const scale = setupMs + runMs || 1;
  return {
    // The axis runs from the start of setup: setup rows from 0, the run's from setupMs (t = 0).
    rows: rows.map((row) => {
      const start = row.kind === "setup" ? row.startMs : setupMs + row.startMs;
      return { ...row, leftPct: (start / scale) * 100, widthPct: (row.durationMs / scale) * 100 };
    }),
    setupMs,
    runMs,
    zeroPct: (setupMs / scale) * 100,
    endLabel: formatMs(runMs),
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
          const { source, outcome: readOutcome, attempts } = event;
          telemetry.push({ source, outcome: readOutcome, attempts });
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
    const state = read.outcome === "tripped" ? "breaker open" : "unavailable";
    const part = `${sourceName(read.source)} ${state}`;
    if (!seen.has(part)) parts.push(part);
    seen.add(part);
  }
  const { repair_rounds: repairs, refine_rounds: refines, failovers } = summary;
  if (repairs) parts.push(plural(repairs, "self-repair round", "self-repair rounds"));
  if (refines) parts.push(plural(refines, "refine round", "refine rounds"));
  if (failovers) parts.push(plural(failovers, "model failover", "model failovers"));
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

// ---------------------------------------------------------------------------------------------
// Guided chapters (#119): GET /scenarios as the page draws it, and which run a scenario shows.

/**
 * GET /scenarios (schemas/api.py ScenarioCatalogue): the page's copy, never a script.
 * @typedef {{ id: string, chapter: string, title: string, caption: string,
 *             setup_notes: string[] }} ScenarioView
 * @typedef {{ chapters: { id: string, title: string, intro: string,
 *                         scenarios: ScenarioView[] }[] }} Catalogue
 */

/**
 * The scenario after this one in catalogue order, across chapters; null after the last.
 * @param {Catalogue} catalogue
 * @param {string} id
 * @returns {ScenarioView | null}
 */
export function nextScenario(catalogue, id) {
  const all = catalogue.chapters.flatMap((chapter) => chapter.scenarios);
  const index = all.findIndex((scenario) => scenario.id === id);
  return index === -1 ? null : (all[index + 1] ?? null);
}

/**
 * The stage's run if it is this scenario's: the scenario played last and, once the trace has
 * started, the trace names it. Otherwise null, so a scenario never shows another run.
 * @param {RunView} run
 * @param {string | null} playedId
 * @param {string} id
 * @returns {RunView | null}
 */
export function scenarioRun(run, playedId, id) {
  if (playedId !== id || run.status === "idle") return null;
  const started = run.trace.events.find((event) => event.kind === "trace_started");
  return started && started.scenario_id !== id ? null : run;
}

/**
 * One recommended action as the report draws it. `raised`: reconcile made it require
 * approval against the model's claim (graph/reconciliation.py).
 * @typedef {{ priority: string, action: string, target: string, type: string,
 *             approval: boolean, raised: boolean }} ReportActionView
 * @typedef {{ kind: "report", summary: string, severity: string, degradation: string,
 *             tone: "ok" | "warn" | "bad", confidence: string, rootCause: string,
 *             breakers: string, provenance: string, actions: ReportActionView[] }
 *   | { kind: "shed", line: string }} ReportView
 */

/** @type {Record<string, "ok" | "warn" | "bad">} */
const DEGRADATION_TONES = { HEALTHY: "ok", PARTIALLY_DEGRADED: "warn" };

/**
 * Where the report's words came from, in one sentence.
 * @param {TraceView} trace
 * @param {Record<string, any>} response
 */
function provenance(trace, response) {
  if (response.cached) {
    const similarity = response.cache_similarity;
    const match = typeof similarity === "number" ? `, similarity ${similarity.toFixed(2)}` : "";
    return `Served from the semantic cache${match}: no run, no model call.`;
  }
  const fellBack =
    trace.visits.some((visit) => visit.node === "fallback_report") ||
    trace.events.some((event) => event.kind === "deadline_exceeded");
  if (fellBack) return "Built by fallback_report from what the run saw: no model text.";
  const model = trace.scripted ? "the scripted model reply" : "the model's reply";
  return `Summary, analysis and actions are ${model}. Status, breakers and approvals are set by reconcile.`;
}

/**
 * The run's answer once trace_finished has arrived: the report, or a shed request's notice.
 * Null before then.
 * @param {TraceView} trace
 * @returns {ReportView | null}
 */
export function reportView(trace) {
  const last = trace.events[trace.events.length - 1];
  if (!trace.finished || last?.kind !== "trace_finished") return null;
  if (!last.response) {
    const slot = trace.events.findLast((event) => event.kind === "run_slot");
    const wait = slot?.retry_after_s ? ` Retry after ${slot.retry_after_s} s.` : "";
    const why =
      slot?.reason === "budget"
        ? "The free-play budget for this minute is spent"
        : "Every run slot was busy";
    return { kind: "shed", line: `No report. ${why}, so the request was shed, not queued.${wait}` };
  }
  const report = last.response.report;
  const raised = new Set(
    trace.events
      .filter((event) => event.kind === "reconciled")
      .flatMap((event) => event.changes.approval_raised),
  );
  /** @type {string[]} */
  const breakers = report.circuit_breakers_tripped;
  return {
    kind: "report",
    summary: report.summary,
    severity: report.severity,
    degradation: report.degradation_status,
    tone: DEGRADATION_TONES[report.degradation_status] ?? "bad",
    confidence: Number(report.confidence_score).toFixed(2),
    rootCause: report.root_cause_analysis,
    breakers: breakers.length ? breakers.map(sourceName).join(", ") : "None",
    provenance: provenance(trace, last.response),
    actions: report.recommended_actions.map(
      (/** @type {Record<string, any>} */ action, /** @type {number} */ index) => ({
        priority: action.priority,
        action: action.action,
        target: action.target_system,
        type: action.action_type,
        approval: action.requires_approval,
        raised: raised.has(index),
      }),
    ),
  };
}

/**
 * One trace event as "Show everything" lists it: when, what kind, and its fields.
 * @typedef {{ seq: number, at: string, kind: string, detail: string }} EventRow
 */

const EVENT_FRAME = new Set(["seq", "t_ms", "kind"]);

/**
 * A field's value in one short token: objects as compact JSON.
 * @param {unknown} value
 */
function fieldText(value) {
  return value !== null && typeof value === "object" ? JSON.stringify(value) : String(value);
}

/**
 * Every event of the trace, in order, for "Show everything". Setup steps and setup reads
 * happen before t = 0 and say "setup"; trace_finished names its outcome, not its response.
 * @param {TraceView} trace
 * @returns {EventRow[]}
 */
export function eventLog(trace) {
  return trace.events.map((event) => {
    const setup = event.kind === "scenario_setup" || event.phase === "setup";
    const fields = Object.entries(event)
      .filter(([name]) => !EVENT_FRAME.has(name))
      .map(([name, value]) =>
        name === "response" ? `response=${value ? "report" : "none"}` : `${name}=${fieldText(value)}`,
      );
    return {
      seq: event.seq,
      at: setup ? "setup" : formatMs(event.t_ms),
      kind: event.kind,
      detail: fields.join(" "),
    };
  });
}
