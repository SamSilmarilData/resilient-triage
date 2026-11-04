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

/**
 * One line saying how a run ended.
 * @param {TraceSummary} summary
 */
export function summaryLine(summary) {
  void summary;
  return "";
}
