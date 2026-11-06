// @ts-check
// The trace-to-view reducer (ADR 0008, #118): the page's one logic module. Everything the page
// shows about a run is derived here from the run's trace events; components only draw what
// these functions return (CODING_STANDARDS.md, Frontend).
import { StreamDataError, StreamHttpError } from "./sse.mjs";

/**
 * One trace event as it arrives on the wire (trace/events.py); `kind` discriminates.
 * @typedef {{ kind: string, seq: number, t_ms: number, [field: string]: any }} TraceEvent
 */

/**
 * Something the page fetches (graph-layout.json, GET /scenarios, GET /graph/nodes): "loading"
 * until it lands, "failed" if it didn't, else the value.
 * @template T
 * @typedef {T | "loading" | "failed"} Loadable
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
 *   simulated: boolean,
 *   visits: NodeVisitView[],
 *   activeNode: string | null,
 *   walked: string[],
 *   finished: boolean,
 * }} TraceView
 * `scripted`: a scenario run, whose model replies are scripted (ADR 0009). `simulated`: a
 * free-play run the keyless simulation answered (no model API key, #153). `walked`: graph
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
function edgeKey(source, target) {
  return `${source}>${target}`;
}

/** @returns {TraceView} The view before any event. */
function initialTrace() {
  return {
    events: [],
    scenarioId: null,
    scripted: false,
    simulated: false,
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
      next.simulated = event.simulated === true; // absent in traces older than #153
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
 * The last event's t_ms, or 0 before any event.
 * @param {TraceEvent[]} events
 */
function lastEventMs(events) {
  return events.length ? events[events.length - 1].t_ms : 0;
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
 * @typedef {{ name: string, terminal: boolean, selectable: boolean, x: number, y: number,
 *             width: number, height: number, r: number, visits: number,
 *             state: "idle" | "visited" | "active", label: string }} GraphNodeView
 * `label`: the node's accessible name, its name and how often the run visited it.
 * `selectable`: the node inspector can open on it (every node but __start__ and __end__).
 * @typedef {{ key: string, source: string, target: string, route: string | null, d: string,
 *             arrow: string, label: { x: number, y: number } | null,
 *             taken: boolean }} GraphEdgeView
 * @typedef {{ viewBox: string, nodePx: number, labelPx: number, nodes: GraphNodeView[],
 *             edges: GraphEdgeView[], visited: number, total: number,
 *             label: string }} GraphView
 * `label`: the graph's accessible name, how many of its nodes the run visited.
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
 * How often a node was visited, as its accessible name says it.
 * @param {number} count
 */
function visitsText(count) {
  if (!count) return "not visited";
  return count === 1 ? "visited once" : `visited ${plural(count, "time", "times")}`;
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
    const terminal = box.r !== undefined;
    return {
      name,
      terminal,
      selectable: !terminal,
      x: box.x,
      y: box.y,
      width: box.width ?? 0,
      height: layout.node_height,
      r: box.r ?? 0,
      visits: count,
      state,
      label: `${name}, ${visitsText(count)}`,
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
  const visited = graphNodes.filter((node) => node.visits).length;
  return {
    viewBox: layout.view_box.join(" "),
    nodePx: layout.font.node_px,
    labelPx: layout.font.label_px,
    nodes,
    // Taken edges last, so they draw over the idle edges they share a corridor with.
    edges: edges.filter((edge) => !edge.taken).concat(edges.filter((edge) => edge.taken)),
    visited,
    total: graphNodes.length,
    label: `Triage graph: ${visited} of ${graphNodes.length} nodes visited`,
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
 *             leftPct: number, widthPct: number,
 *             detailParts: string[] }} WaterfallRow
 * @typedef {{ rows: WaterfallRow[], setupMs: number, runMs: number, zeroPct: number,
 *             endLabel: string,
 *             showZeroLabel: boolean }} WaterfallView
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
 * The run as waterfall rows, each placed on one axis from the start of setup to the run's end;
 * null before the run's first event, when there is nothing to draw.
 * @param {TraceView} trace
 * @param {number} [trackPx] how wide the axis's track is drawn: the zero label hides if it
 *   would meet the end label there
 * @param {string | null} [normalBackend] the cache backend normal for this run
 *   (normalCacheBackend()): cache rows name theirs only when it differs; null names none.
 *   Without one, the deployment's normal backend, Redis with vector search
 * @returns {WaterfallView | null}
 */
export function waterfallView(
  trace,
  trackPx = NARROWEST_TRACK_PX,
  normalBackend = DEPLOYED_BACKEND,
) {
  if (!trace.events.length) return null;
  const lastMs = Math.max(0, lastEventMs(trace.events));
  /** @typedef {Omit<WaterfallRow, "leftPct" | "widthPct">} PlacedRow */
  /** @type {PlacedRow[]} */
  const rows = [];
  let setupCursor = 0; // where the next setup read starts: setup reads lie end to end from 0
  let stepStart = 0; // where the current setup step's first read started
  let stepRow = 0; // the row index of the current setup step's first read
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
        listStepFirst(rows, stepRow);
        stepStart = setupCursor;
        stepRow = rows.length;
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
        const detail = withBackend(
          e.hit ? `hit · similarity ${e.similarity.toFixed(2)}` : "miss",
          e.backend,
          normalBackend,
        );
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
          tag: modelReplyTag(trace),
          tone: completed ? "ok" : "warn",
          ...endedAt(e, e.duration_ms),
        });
        break;
      }
      case "cache_store": {
        const tone = e.outcome === "stored" ? "ok" : "warn";
        const span = endedAt(e, e.duration_ms);
        const detail = withBackend(words(e.outcome), e.backend, normalBackend);
        add(e, { kind: "store", name: "cache store", detail, tone, ...span });
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
  const axis = waterfallAxis(hasSetup, setupCursor, runMs);
  const endLabel = formatMs(runMs);
  return {
    // The axis runs from the start of setup: setup rows from 0, the run's from zeroPct (t = 0).
    rows: rows.map((row) => ({
      ...row,
      ...axis.place(row.kind === "setup", row.startMs, row.durationMs),
      detailParts: row.detail.split(DETAIL_SEPARATOR),
    })),
    setupMs: axis.setupMs,
    runMs,
    zeroPct: axis.zeroPct,
    endLabel,
    showZeroLabel: !axisLabelsCollide(axis.zeroPct, endLabel, trackPx),
  };
}

// --- #159: readable timeline ---
// Setup draws on a scale of its own, compressed to at most MAX_SETUP_SHARE of the axis, so a
// long setup never squeezes the run; shorter, it keeps the run's clock. Setup steps list before
// the reads they made. The zero tick's label hides rather than meet the end label.

// The most of the axis setup may take: the run keeps at least the rest.
export const MAX_SETUP_SHARE = 0.25;
// The narrowest the waterfall's track is drawn (a 320 px phone): label collisions are judged on it.
export const NARROWEST_TRACK_PX = 120;
// Axis tick text (styles.css `.waterfall__tick`): 0.7rem JetBrains Mono, 0.6em per character,
// inset 0.25rem from the zero line; labels closer than LABEL_GAP_PX read as one.
const TICK_CHAR_PX = 0.7 * 16 * 0.6;
const TICK_INSET_PX = 4;
const LABEL_GAP_PX = 4;
const ZERO_LABEL = "0";
const DETAIL_SEPARATOR = " · ";

// --- Cache backend: rows name it only when it isn't the run's normal one ---

// What a cache row calls the backend that served it.
const BACKEND_NAMES = /** @type {Record<string, string>} */ ({
  redisearch: "Redis",
  redis_hash_fallback: "Redis without search",
  in_memory: "in memory",
});
// A deployment's normal cache backend: Redis with vector search.
const DEPLOYED_BACKEND = "redisearch";
// A scenario setup note saying Redis is down (Everything down's). The trace records no setup
// step for it: every scenario's cache is private and in memory (ADR 0009), so the catalogue's
// note is the only place a scenario says it means Redis down.
const REDIS_DOWN_NOTE = /\bRedis is (?:unreachable|down)\b/;

/**
 * GET /health as the page reads it: only the deployment's active cache driver.
 * @typedef {{ cache_driver: string }} HealthView
 */

/**
 * A cache row's detail, naming the backend that served it when it isn't the normal one.
 * @param {string} detail
 * @param {string} backend
 * @param {string | null} normalBackend null: name none
 */
function withBackend(detail, backend, normalBackend) {
  if (normalBackend === null || backend === normalBackend) return detail;
  return `${detail}${DETAIL_SEPARATOR}${BACKEND_NAMES[backend] ?? words(backend)}`;
}

/**
 * The cache backend normal for a run, against which its cache rows name theirs. A scenario
 * run's private in-memory cache stands in for the deployment's (ADR 0009), so it is normal
 * unless the scenario's setup takes Redis down; then the deployment's Redis is the normal
 * one it lost. A free-play run uses the deployment's cache, so its normal backend is the one
 * GET /health reports. Null (name none) while that or the catalogue is unknown.
 * @param {TraceView} trace
 * @param {Loadable<Catalogue>} catalogue
 * @param {Loadable<HealthView>} health
 * @returns {string | null}
 */
export function normalCacheBackend(trace, catalogue, health) {
  if (trace.scenarioId === null) return typeof health === "object" ? health.cache_driver : null;
  const scenario = catalogueScenario(catalogue, trace.scenarioId);
  const redisDown = scenario?.setup_notes.some((note) => REDIS_DOWN_NOTE.test(note));
  return redisDown ? DEPLOYED_BACKEND : null;
}

/**
 * Move the setup step just added (the last row) up to `at`, ahead of the reads it made from
 * there on, and indent those reads under it. Setup comes before any node visit is recorded.
 * @template {{ depth: 0 | 1 }} Row
 * @param {Row[]} rows
 * @param {number} at
 */
function listStepFirst(rows, at) {
  const step = /** @type {Row} */ (rows.pop());
  for (let i = at; i < rows.length; i++) rows[i] = { ...rows[i], depth: 1 };
  rows.splice(at, 0, step);
}

/**
 * The waterfall's axis: setup's share of it, and where a row's bar goes, in percent.
 * @param {boolean} hasSetup
 * @param {number} setupSpanMs setup reads laid end to end
 * @param {number} runMs
 */
export function waterfallAxis(hasSetup, setupSpanMs, runMs) {
  const linear = setupSpanMs + runMs > 0 ? setupSpanMs / (setupSpanMs + runMs) : 1;
  const share = hasSetup ? Math.min(MAX_SETUP_SHARE, Math.max(MIN_SETUP_SHARE, linear)) : 0;
  // Setup's own clock: its real span, or the run's clock when setup is shorter than the minimum.
  const setupMs = hasSetup ? Math.max(setupSpanMs, (share / (1 - share)) * runMs) : 0;
  const setupScale = setupMs || 1;
  const runScale = runMs || 1;
  return {
    setupMs,
    zeroPct: share * 100,
    /**
     * @param {boolean} setup whether the row is setup's (else the run's, from t = 0)
     * @param {number} startMs
     * @param {number} durationMs
     */
    place(setup, startMs, durationMs) {
      if (setup) {
        const unit = (share * 100) / setupScale;
        return { leftPct: startMs * unit, widthPct: durationMs * unit };
      }
      const unit = ((1 - share) * 100) / runScale;
      return { leftPct: share * 100 + startMs * unit, widthPct: durationMs * unit };
    },
  };
}

/**
 * Whether the zero tick's label and the end label would meet on a track `trackPx` wide: the
 * zero label starts just right of the zero line, the end label ends at the track's end.
 * @param {number} zeroPct
 * @param {string} endLabel
 * @param {number} trackPx
 */
export function axisLabelsCollide(zeroPct, endLabel, trackPx) {
  const zeroEnd = (zeroPct / 100) * trackPx + TICK_INSET_PX + ZERO_LABEL.length * TICK_CHAR_PX;
  const endStart = trackPx - endLabel.length * TICK_CHAR_PX;
  return zeroEnd + LABEL_GAP_PX > endStart;
}

// ---------------------------------------------------------------------------------------------
// Summary: the JS port of trace/summary.py, checked against it by tests/browser/test_reducer.py.

// Node names as registered in graph/builder.py, as trace/summary.py names them;
// tests/browser/test_reducer.py fails if they drift.
export const SELF_REPAIR_NODE = "self_repair";
export const REFINE_NODE = "refine_reflection";

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

  const lastMs = lastEventMs(events); // 0 when there are none, as originMs then is
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
// `display_name`; tests/browser/test_reducer.py fails if they drift). An unknown id shows as
// itself.
export const SOURCE_NAMES =/** @type {Record<string, string>} */ ({
  statuspage: "Statuspage",
  chaos: "Cluster Sentinel",
});

/**
 * A source's display name.
 * @param {string} source
 */
function sourceName(source) {
  return SOURCE_NAMES[source] ?? source;
}

/**
 * A duration for people: "under 1 ms", "917 ms", "1.8 s".
 * @param {number} ms
 */
function formatMs(ms) {
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
  // #162: the deadline's fallback is the gateway's, not fallback_report, so the lit path
  // never shows it; the line says where the answer came from.
  if (summary.deadline_exceeded) parts.push("run deadline hit, answered outside the graph");
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
const STREAM_WATCHDOG_MS = 30_000;

/**
 * idle: nothing played yet. streaming: events arriving. finished: trace_finished arrived
 * (a cache hit, an analyzed run or a shed alike). lost: the stream went silent or closed
 * early; the partial trace stays. error: the stream was refused, never connected, or sent data
 * that didn't parse (#163; any partial trace stays).
 * `skipping`: Skip to end was pressed during this stream; the controller folds its events
 * without reporting them until the run ends, so the stage holds still and then shows the
 * final state at once. `startedAt`: the wall-clock ms (the controller's clock) the run
 * started at, null before any run; the streaming headline counts from it (#156).
 * `rejected`: the server refused the request before any stream (an HTTP error status), so
 * no graph ran (#160).
 * @typedef {"idle" | "streaming" | "finished" | "lost" | "error"} RunStatus
 * @typedef {{ status: RunStatus, trace: TraceView, error: string | null,
 *             skipping: boolean,
 *             startedAt: number | null,
 *             rejected: boolean }} RunView
 * @typedef {import("./sse.mjs").StreamSource} StreamSource
 */

/**
 * What the stage says about a run: its one line, whether to dim the partial trace, the
 * action its button offers (Replay after a lost stream, Retry after a refusal), and whether
 * its model replies are scripted (a scenario run that called a model, ADR 0009; #161).
 * @typedef {{ status: RunStatus, line: string | null, dimmed: boolean,
 *             action: "Replay" | "Retry" | null, scripted: boolean }} RunHeadline
 * @param {RunView} run
 * @param {number} [now] wall-clock ms on the run's clock, for a streaming run's headline
 * @returns {RunHeadline}
 */
export function runHeadline(run, now) {
  return { ...runLine(run, now), scripted: run.trace.scripted && calledAModel(run.trace) };
}

// --- #161: scripted badge only on runs that called a model ---

/**
 * Whether a trace records at least one model call: a cache hit or a shed request never
 * calls one, so it has no reply to call scripted.
 * @param {TraceView} trace
 */
function calledAModel(trace) {
  return modelCallEvents(trace).length > 0;
}

/**
 * The trace's model_call events, in order.
 * @param {TraceView} trace
 */
function modelCallEvents(trace) {
  return trace.events.filter((event) => event.kind === "model_call");
}

/**
 * The headline's status, line, dimming and action.
 * @param {RunView} run
 * @param {number} [now]
 * @returns {Omit<RunHeadline, "scripted">}
 */
function runLine(run, now) {
  const { status } = run;
  switch (status) {
    case "idle":
      return { status, line: null, dimmed: false, action: null };
    case "streaming":
      if (run.skipping) return { status, line: "Skipping to the end", dimmed: false, action: null };
      return {
        status,
        line: `Running, ${formatMs(runElapsedMs(run, now))} in`,
        dimmed: false,
        action: null,
      };
    case "finished":
      return { status, line: summaryLine(summarize(run.trace.events)), dimmed: false, action: null };
    case "lost":
      return { status, line: "Connection lost", dimmed: true, action: "Replay" };
    case "error":
      return { status, line: run.error, dimmed: false, action: "Retry" };
  }
}

/**
 * What a stage's live region says about a run: only how it ended, never the running tick,
 * so a screen reader hears one announcement per run (#122). Empty until then.
 * @param {RunView} run
 * @returns {string}
 */
export function runAnnouncement(run) {
  const { line } = runHeadline(run);
  switch (run.status) {
    case "finished":
      return `Run finished: ${line}`;
    case "lost":
      return `Run stopped: ${line}`;
    case "error":
      return `Run failed: ${line}`;
    default:
      return "";
  }
}

/**
 * Whether the stage needs scrolling into view for a run that just started: its top is above
 * the viewport or below its bottom. At phone width it sits below the chapters (#122); pinned
 * on desktop, it never needs it.
 * @param {{ top: number }} rect the stage's bounding rectangle
 * @param {number} viewportHeight
 */
export function needsReveal(rect, viewportHeight) {
  return rect.top < 0 || rect.top >= viewportHeight;
}

/** @returns {RunView} */
export function idleRun() {
  return {
    status: "idle",
    trace: initialTrace(),
    error: null,
    skipping: false,
    startedAt: null,
    rejected: false,
  };
}

// --- #156: the run's clock ---

/**
 * How long a streaming run has been going: wall time since it started, never less than its
 * last event's own `t_ms` (a `now` sampled before that event landed). Without a `now` or a
 * start, the last event's `t_ms`.
 * @param {RunView} run
 * @param {number} [now]
 */
function runElapsedMs(run, now) {
  const lastMs = lastEventMs(run.trace.events);
  if (now === undefined || run.startedAt === null) return lastMs;
  return Math.max(lastMs, now - run.startedAt);
}

/**
 * Whether the page's clock should tick for this run: only while it streams and its headline
 * counts (not while skipping to the end, which holds the stage still).
 * @param {RunView} run
 */
export function runTicks(run) {
  return run.status === "streaming" && !run.skipping;
}

const UNREACHABLE = "Couldn't reach the server. Check your connection and try again.";
// --- #163: malformed streams ---
/** The stream arrived but didn't parse: a different failure from an unreachable server. */
const MALFORMED = "Couldn't read the run's data: the server's stream didn't parse. Try again.";

/**
 * Plays stream sources into RunViews, reporting each new view to `onChange`. One run at a
 * time: play() supersedes the run before it and resolves with the run's last view. Skip to
 * end (skipToEnd(), or play with `skipping`) stops reporting the views a stream's events make
 * until the run ends: the stream is real time, so the end can't come sooner, but nothing in
 * between is drawn. `view` is always the latest fold. `clock` stamps each run's `startedAt`
 * (wall-clock ms); the page's ticking `now` must read the same clock.
 * @param {{ onChange: (view: RunView) => void, watchdogMs?: number,
 *           clock?: () => number }} options
 */
export function createRunController({
  onChange,
  watchdogMs = STREAM_WATCHDOG_MS,
  clock = Date.now,
}) {
  let view = idleRun();
  let runId = 0;
  /** @type {AbortController | null} */
  let current = null;
  /** @type {StreamSource | null} */
  let lastSource = null;

  /**
   * @param {RunView} next
   * @param {boolean} [quiet] keep the view without reporting it
   */
  const set = (next, quiet = false) => {
    view = next;
    if (!quiet) onChange(view);
  };

  /**
   * Play a source from the start; resolves when the run ends, however it ends.
   * @param {StreamSource} source
   * @param {{ skipping?: boolean }} [options] `skipping`: start already skipped to the end
   * @returns {Promise<RunView>}
   */
  async function play(source, { skipping = false } = {}) {
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
     * @param {boolean} [rejected] the server refused the request before any stream (#160)
     */
    const end = (status, error = null, rejected = false) => {
      clearTimeout(timer);
      if (!live()) return;
      runId++; // anything the old stream still yields is ignored from here
      current = null;
      // A finished stream closes itself once trace_finished is sent; only cut a stalled one.
      if (status !== "finished") abort.abort();
      set({ ...view, status, error, skipping: false, rejected });
    };
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(() => end("lost"), watchdogMs);
    };

    set({
      status: "streaming",
      trace: initialTrace(),
      error: null,
      skipping,
      startedAt: clock(),
      rejected: false,
    });
    arm();
    try {
      for await (const message of source(abort.signal)) {
        if (!live()) break;
        arm();
        if (message.kind !== "event") continue;
        const trace = reduceTrace(view.trace, message.event);
        set({ ...view, trace }, view.skipping);
        if (trace.finished) {
          end("finished");
          break;
        }
      }
      end("lost"); // closed before trace_finished
    } catch (error) {
      if (error instanceof StreamHttpError) end("error", error.message, true);
      else if (error instanceof StreamDataError) end("error", MALFORMED);
      else if (view.trace.events.length) end("lost");
      else end("error", UNREACHABLE);
    }
    return view;
  }

  return {
    play,
    /**
     * Skip to end: hold the stage as it is until the streaming run ends, then show its final
     * state. A no-op unless a run is streaming and not already skipped.
     */
    skipToEnd() {
      if (view.status === "streaming" && !view.skipping) set({ ...view, skipping: true });
    },
    /** Play the last source again from the start (the Replay and Retry buttons). */
    replay() {
      return lastSource ? play(lastSource) : Promise.resolve(view);
    },
    /** The current view. */
    get view() {
      return view;
    },
  };
}

// --- #157: pinned stage reveal ---

/**
 * Where to scroll the stage itself for a run that just started, so its graph shows: back to
 * its top when the pinned stage scrolls inside itself (#157) and has been scrolled down, else
 * null (below the wide breakpoint the stage does not scroll; needsReveal() moves the page).
 * @param {{ scrollTop: number, scrollHeight: number, clientHeight: number }} stage
 * @returns {number | null}
 */
export function stageRevealTop(stage) {
  return stage.scrollHeight > stage.clientHeight && stage.scrollTop > 0 ? 0 : null;
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
 * The scenario open after its toggle is pressed: it closes if it was open, else it opens
 * (and any other closes; one is open at a time).
 * @param {string | null} openId
 * @param {string} id
 * @returns {string | null}
 */
export function toggledOpen(openId, id) {
  return openId === id ? null : id;
}

/**
 * What a scenario's Skip to end does, given its run (scenarioRun()): "skip" holds a streaming
 * run until it ends, "start" plays the scenario already skipped (no run yet, or it was lost
 * or refused), null does nothing (the run is finished or already skipped). Once another
 * scenario has played on the stage, an earlier scenario shows its kept finished run
 * (keepRun()), so its Skip to end has nothing to do.
 * @param {RunView | null} run
 * @returns {"skip" | "start" | null}
 */
export function skipAction(run) {
  if (run === null || run.status === "lost" || run.status === "error") return "start";
  if (run.status === "streaming" && !run.skipping) return "skip";
  return null;
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
 * The guided scenarios on the stage's run controller: Play and Skip to end by scenario id,
 * and which run each scenario shows. It owns the id of the scenario played last and each
 * scenario's last finished run (#161).
 * @param {ReturnType<typeof createRunController>} controller
 * @param {(id: string) => StreamSource} sourceOf the stream of one scenario run
 */
export function createScenarioStage(controller, sourceOf) {
  /** @type {string | null} */
  let playedId = null;
  /** @type {Map<string, RunView>} */
  const kept = new Map();
  /**
   * @param {RunView} run
   * @param {string} id
   */
  const shown = (run, id) => scenarioRun(run, playedId, id) ?? kept.get(id) ?? null;

  /**
   * @param {string} id
   * @param {{ skipping?: boolean }} [options]
   */
  const start = (id, options) => {
    playedId = id;
    return controller.play(sourceOf(id), options).then((view) => {
      keepRun(kept, id, view);
      return view;
    });
  };

  return {
    /**
     * This scenario's run: `run` (the stage's, as last drawn) if it is this scenario's
     * (scenarioRun()), else its last finished run, if it has one.
     * @param {RunView} run
     * @param {string} id
     */
    runOf(run, id) {
      return shown(run, id);
    },
    /**
     * Whether a run of this scenario has finished on the stage (so Play is "Play again").
     * @param {string} id
     */
    finishedOnce(id) {
      return kept.has(id);
    },
    /**
     * Play a scenario on the stage from the start.
     * @param {string} id
     */
    play(id) {
      return start(id);
    },
    /**
     * Skip to end for a scenario, as skipAction() says: hold its streaming run, or play it
     * already skipped.
     * @param {string} id
     */
    skip(id) {
      const action = skipAction(shown(controller.view, id));
      if (action === "skip") controller.skipToEnd();
      if (action === "start") start(id, { skipping: true });
    },
  };
}

// --- #161: scenario panels keep their results ---

/**
 * Keep a scenario's run as its last result once it has finished. A run another play
 * superseded, or one that was lost or refused, leaves the kept one as it was.
 * @param {Map<string, RunView>} kept last finished run by scenario id
 * @param {string} id
 * @param {RunView} view the stage's view when this scenario's play() resolved
 */
function keepRun(kept, id, view) {
  if (view.status === "finished" && view.trace.scenarioId === id) kept.set(id, view);
}

/**
 * The Play button's label: "Play" until a run of the scenario has finished, then "Play
 * again", including while its next run streams.
 * @param {RunView | null} run the scenario's run (runOf())
 * @param {boolean} finishedOnce
 * @returns {"Play" | "Play again"}
 */
function playLabel(run, finishedOnce) {
  return finishedOnce || run?.status === "finished" ? "Play again" : "Play";
}

/**
 * What an opened scenario shows of its run: the run's line, the Play button's label, whether
 * Skip to end has anything left to do, and once the run has finished, its report (or shed
 * notice) and every event for "Show everything".
 * @typedef {{ line: string | null, playLabel: "Play" | "Play again", skipDisabled: boolean,
 *             report: ReportView | null, events: EventRow[] }} ScenarioPanelView
 * @param {RunView | null} run the scenario's run (runOf()), null if it has none
 * @param {boolean} [finishedOnce] a run of this scenario has finished before (finishedOnce())
 * @param {number} [now] the page's ticking clock, so a streaming run's line counts (#156)
 * @returns {ScenarioPanelView}
 */
export function scenarioPanelView(run, finishedOnce = false, now) {
  const report = run && reportView(run.trace);
  return {
    line: run && runHeadline(run, now).line,
    playLabel: playLabel(run, finishedOnce),
    skipDisabled: skipAction(run) === null,
    report,
    events: run && report ? eventLog(run.trace) : [],
  };
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
  const model = REPLY_SOURCES[replyKind(trace)];
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
    const shed = shedNotice(trace);
    return { kind: "shed", line: `No report: the request was shed, not queued. ${shed?.line ?? ""}`.trim() };
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

// --- #158: the stage names its scenario ---

/**
 * The scenario the stage is drawing, as the stage names it: its id and catalogue title, once
 * the run's trace has started and names a catalogued scenario; null otherwise (an idle stage,
 * a free-play run, or the catalogue not loaded). `offerBack`: the run has finished, so the
 * stage offers a way back to the scenario's card and report (shown only at narrow widths,
 * where the card sits far above the stage).
 * @typedef {{ id: string, title: string, offerBack: boolean }} StageScenario
 * @param {Loadable<Catalogue>} catalogue
 * @param {RunView} run
 * @returns {StageScenario | null}
 */
export function stageScenario(catalogue, run) {
  const id = run.trace.scenarioId;
  const scenario = id === null ? null : catalogueScenario(catalogue, id);
  if (id === null || !scenario) return null;
  return { id, title: scenario.title, offerBack: run.status === "finished" };
}

/**
 * A catalogued scenario by id; null if it isn't there or the catalogue hasn't loaded.
 * @param {Loadable<Catalogue>} catalogue
 * @param {string} id
 * @returns {ScenarioView | null}
 */
function catalogueScenario(catalogue, id) {
  if (typeof catalogue !== "object") return null;
  const scenarios = catalogue.chapters.flatMap((chapter) => chapter.scenarios);
  return scenarios.find((candidate) => candidate.id === id) ?? null;
}

// ---------------------------------------------------------------------------------------------
// Node inspector (#121): GET /graph/nodes, projected for the inspector and the node list.
// Paths on the wire are repository-relative; links open them on GitHub.

/**
 * One node as GET /graph/nodes serves it (schemas/api.py NodeInspectorView).
 * @typedef {{ name: string, purpose: string, file: string,
 *             config_fields: { name: string, description: string }[], adrs: string[] }} GraphNode
 */

/**
 * @typedef {{
 *   name: string,
 *   status: "ready" | "loading" | "failed",
 *   purpose: string | null,
 *   file: { path: string, url: string } | null,
 *   configFields: { name: string, description: string }[],
 *   adrs: { label: string, file: string, url: string }[],
 * }} InspectorView
 * `status` is the node list's: purpose, file and the lists stay empty until it is "ready".
 */

/** Where the repository's files are browsed. */
const REPO_BLOB_URL = "https://github.com/SamSilmarilData/resilient-triage/blob/main/";

/**
 * A repository-relative path's URL on GitHub.
 * @param {string} path
 */
function repoUrl(path) {
  return REPO_BLOB_URL + path;
}

/**
 * What the inspector shows for a node, or null when none is selected.
 * @param {Loadable<GraphNode[]>} nodes
 * @param {string | null} name
 * @returns {InspectorView | null}
 */
export function inspectorView(nodes, name) {
  if (name === null) return null;
  const node = Array.isArray(nodes) ? nodes.find((n) => n.name === name) : undefined;
  const status = Array.isArray(nodes) ? "ready" : nodes;
  if (!node) return { name, status, purpose: null, file: null, configFields: [], adrs: [] };
  return {
    name,
    status,
    purpose: node.purpose,
    file: { path: node.file, url: repoUrl(node.file) },
    configFields: node.config_fields,
    adrs: node.adrs.map((path) => {
      const file = path.slice(path.lastIndexOf("/") + 1);
      return { label: adrLabel(file.slice(0, 4)), file, url: repoUrl(path) };
    }),
  };
}

// --- #165: ADR links name their record ---

/**
 * Each decision record's title by its number, as its `# ADR-00N: Title` heading reads.
 * GET /graph/nodes serves only paths and docs/ isn't in the wheel, so the page keeps this copy;
 * tests/browser/test_how_its_built.py checks it against docs/decisions/.
 * @type {Readonly<Record<string, string>>}
 */
export const ADR_TITLES = Object.freeze({
  "0001": "LangGraph Cyclic State Machine for Incident Triage",
  "0002": "Dual-Engine Semantic Caching (Redis 8 RediSearch HNSW with NumPy Fallback)",
  "0003": "Pybreaker and Tenacity for Fault-Tolerant Upstream Telemetry",
  "0004": "Strict Pydantic Contracts and Automated Self-Repair Loop",
  "0005": "Free-Tier Groq Quota Monitoring and Multi-Model Failover",
  "0006": "LLM Access Through a Project-Owned Gateway That Returns Failures as Values",
  "0007": "No Checkpointing on the Request Path",
  "0008": "Execution Trace from a Per-Request Recorder, Streamed over SSE",
  "0009": "Scenario Runs Are Fully Isolated from the Shared Runtime",
  "0010": "Build-Free, Vendored Frontend",
});

/**
 * An ADR link's label: its number and, when known, its title.
 * @param {string} number four digits, e.g. "0006"
 */
function adrLabel(number) {
  const title = ADR_TITLES[number];
  return title ? `ADR ${number}: ${title}` : `ADR ${number}`;
}

// ---------------------------------------------------------------------------------------------
// Free play (#120): the request a visitor's form makes, and what the free-play panel says
// about its run beyond the stage's headline.

/**
 * The POST /triage/stream body from the free-play form's fields: the question trimmed, an
 * empty service filter left out.
 * @param {{ query: string, service_filter: string, force_refresh: boolean }} fields
 * @returns {import("./sse.mjs").TriageRequest}
 */
export function freePlayRequest({ query, service_filter, force_refresh }) {
  const filter = service_filter.trim();
  return { query: query.trim(), service_filter: filter || null, force_refresh };
}

/**
 * The model that wrote the run's report: the last model call that completed, or null when
 * none did (a cache hit, a shed, or no model answered).
 * @param {TraceView} trace
 * @returns {string | null}
 */
export function answeringModel(trace) {
  const answered = modelCallEvents(trace).findLast((call) => call.outcome === "completion");
  return answered?.model ?? null;
}

/**
 * Why a run was shed and when to ask again, from its refused run_slot; null when it wasn't
 * shed. Only a budget shed carries a retry-after (triage/gateway.py Shed).
 * @typedef {{ reason: string, retryAfterS: number | null, line: string }} ShedNotice
 * @param {TraceView} trace
 * @returns {ShedNotice | null}
 */
export function shedNotice(trace) {
  const slot = trace.events.find((e) => e.kind === "run_slot" && !e.acquired);
  if (!slot) return null;
  const reason = slot.reason ?? "capacity";
  const retryAfterS = slot.retry_after_s ?? null;
  const head =
    reason === "budget"
      ? "The free-play budget is spent for this minute."
      : "Every run slot is busy right now.";
  const when = retryAfterS === null ? "in a moment" : `in ${retryAfterS} s`;
  return { reason, retryAfterS, line: `${head} Try again ${when}.` };
}

// --- #153: keyless simulation ---

/**
 * Where a run's model replies come from: scripted (a scenario run, ADR 0009), the keyless
 * simulation (free play with no model API key, #153), or a real provider.
 * @typedef {"scripted" | "simulated" | "real"} ReplyKind
 * @param {TraceView} trace
 * @returns {ReplyKind}
 */
function replyKind(trace) {
  if (trace.scripted) return "scripted";
  return trace.simulated ? "simulated" : "real";
}

/** The report's provenance names its model text by where the replies came from. */
const REPLY_SOURCES = /** @type {Record<ReplyKind, string>} */ ({
  scripted: "the scripted model reply",
  simulated: "the keyless simulation's reply",
  real: "the model's reply",
});

/** The waterfall's tag on a model call; a real provider's reply has none. */
const REPLY_TAGS = /** @type {Record<ReplyKind, string | null>} */ ({
  scripted: "scripted reply",
  simulated: "simulated reply",
  real: null,
});

/**
 * The waterfall tag on a model call: scenario runs are scripted, keyless free play is the
 * simulation, and a real provider's reply has no tag.
 * @param {TraceView} trace
 * @returns {string | null}
 */
export function modelReplyTag(trace) {
  return REPLY_TAGS[replyKind(trace)];
}

/**
 * Free play's model line: who wrote the run's report, or null when no model answered.
 * @param {TraceView} trace
 * @returns {string | null}
 */
export function modelLine(trace) {
  const model = answeringModel(trace);
  if (model === null) return null;
  return replyKind(trace) === "simulated"
    ? "Model: the keyless simulation (no model API key is set)"
    : `Model: ${model}`;
}

// --- #155: free play shows its report ---

/**
 * What the free-play panel draws beside its stage: the model line, a shed run's notice, and
 * the finished run's report as a scenario panel reads it (a cache hit's is the cached one).
 * A shed run has no report: its notice already says why. With a wall-clock `now`, a budget
 * shed's notice counts down to its retry time, and the view says whether the panel was
 * refused and whether Triage may ask again (#160).
 * @typedef {{ model: string | null, shed: ShedNotice | null,
 *             report: ReportView | null, refused: boolean, retryAt: number | null,
 *             canTriage: boolean }} FreePlayView
 * @param {RunView} run
 * @param {number} [now] wall-clock ms on the run's clock
 * @returns {FreePlayView}
 */
export function freePlayView(run, now) {
  const report = reportView(run.trace);
  return {
    model: modelLine(run.trace),
    shed: shedNotice(run.trace),
    report: report?.kind === "report" ? report : null,
    ...refusalView(run, now),
  };
}

// --- #160: free play's input, double submits and refusals ---

/** TriageRequest's shortest question (schemas/api.py), counted after trimming. */
export const QUERY_MIN_LENGTH = 3;
/** What the form says about a question too short to send. */
const QUERY_TOO_SHORT = `Describe the incident in at least ${QUERY_MIN_LENGTH} characters.`;

/**
 * What a Triage press does: the request to send, or why nothing is sent. A question shorter
 * than the minimum once trimmed gets a plain message (not the server's validation error); a
 * press while the panel's run streams, or before a budget shed's retry time (a double click),
 * sends nothing and says nothing.
 * @param {{ query: string, service_filter: string, force_refresh: boolean }} fields
 * @param {RunView} run the panel's run as it is now
 * @param {number} now wall-clock ms on the run's clock
 * @returns {{ error: string | null, request: import("./sse.mjs").TriageRequest | null }}
 */
export function freePlaySubmit(fields, run, now) {
  const request = freePlayRequest(fields);
  if (request.query.length < QUERY_MIN_LENGTH) return { error: QUERY_TOO_SHORT, request: null };
  if (!canTriage(run, now)) return { error: null, request: null };
  return { error: null, request };
}

/**
 * When a budget-shed run may ask again: when its refused run_slot arrived (the run's start
 * plus the event's t_ms) plus its retry-after. Null for any other run.
 * @param {RunView} run
 * @returns {number | null}
 */
function retryDeadline(run) {
  const slot = run.trace.events.find((e) => e.kind === "run_slot" && !e.acquired);
  if (!slot || slot.retry_after_s == null || run.startedAt === null) return null;
  return run.startedAt + slot.t_ms + slot.retry_after_s * 1000;
}

/**
 * Whether Triage may send a new run: not while one streams, nor before a budget shed's
 * retry time.
 * @param {RunView} run
 * @param {number | undefined} now
 */
function canTriage(run, now) {
  if (run.status === "streaming") return false;
  const retryAt = retryDeadline(run);
  return retryAt === null || now === undefined || now >= retryAt;
}

/**
 * Whether the page's clock should tick for the free-play panel: while a budget shed's
 * countdown runs.
 * @param {RunView} run
 * @param {number} now
 */
export function freePlayTicks(run, now) {
  const retryAt = retryDeadline(run);
  return retryAt !== null && now < retryAt;
}

/**
 * The refusal parts of the free-play view: whether the run was refused before any graph ran
 * (shed, or rejected by the server), its retry deadline, whether Triage may ask again, and a
 * budget shed's notice line counted down to `now`.
 * @param {RunView} run
 * @param {number | undefined} now
 * @returns {Pick<FreePlayView, "refused" | "retryAt" | "canTriage"> &
 *           { shed?: ShedNotice }}
 */
function refusalView(run, now) {
  const shed = shedNotice(run.trace);
  const retryAt = retryDeadline(run);
  const parts = {
    refused: shed !== null || run.rejected,
    retryAt,
    canTriage: canTriage(run, now),
  };
  if (!shed || retryAt === null || shed.retryAfterS === null || now === undefined) return parts;
  return { ...parts, shed: { ...shed, line: countdownLine(shed.retryAfterS, retryAt, now) } };
}

/**
 * A budget shed's notice line at `now`: whole seconds left to its retry time (never more than
 * the retry-after it was given), then "try again now" once it has passed.
 * @param {number} retryAfterS
 * @param {number} retryAt
 * @param {number} now
 */
function countdownLine(retryAfterS, retryAt, now) {
  const left = Math.min(retryAfterS, Math.ceil((retryAt - now) / 1000));
  if (left <= 0) return "The free-play budget was spent for that minute. Try again now.";
  return `The free-play budget is spent for this minute. Try again in ${left} s.`;
}
