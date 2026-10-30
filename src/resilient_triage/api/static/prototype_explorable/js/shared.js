// @ts-check
// PROTOTYPE (issue #96), throwaway. Shared pieces every variant may use: trace loading,
// the player clock, plain-language event sentences, the graph, the waterfall, the summary
// line, the event log and the report. Layout belongs to each variant, not here.
import { html } from "htm/preact";
import { signal, computed, effect } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";

// ---------------------------------------------------------------- data

export const data = signal(/** @type {any} */ (null));
export const scenarioId = signal(readHash() || "healthy");
export const selectedSeq = signal(/** @type {number|null} */ (null));

function readHash() {
  const h = location.hash.slice(1);
  return /^[a-z-]+$/.test(h) ? h : "";
}
window.addEventListener("hashchange", () => {
  const h = readHash();
  if (h && h !== scenarioId.value) pick(h);
});

export async function load() {
  const res = await fetch("traces.json");
  data.value = await res.json();
  checkLayout();
  restart();
}

export const CHAPTERS = ["Fast paths", "The model misbehaves", "Dependencies fail", "Finale"];

export const scenario = computed(() =>
  data.value?.scenarios.find((/** @type {any} */ s) => s.id === scenarioId.value) ?? data.value?.scenarios[0],
);

/** Times rebased so trace_started is 0; setup events are negative. */
export const trace = computed(() => {
  const sc = scenario.value;
  if (!sc) return null;
  const start = sc.events.find((/** @type {any} */ e) => e.kind === "trace_started");
  const t0 = start.t_ms;
  const events = sc.events.map((/** @type {any} */ e) => ({
    ...e,
    t: e.t_ms - t0,
    s: (e.started_ms ?? e.t_ms - (e.waited_ms ?? 0)) - t0,
  }));
  const finished = events[events.length - 1];
  const minT = Math.min(0, ...events.map((/** @type {any} */ e) => e.s));
  return { sc, events, finished, minT, end: finished.t, response: finished.response };
});

export function pick(/** @type {string} */ id) {
  scenarioId.value = id;
  selectedSeq.value = null;
  if (location.hash.slice(1) !== id) history.replaceState(null, "", `${location.search}#${id}`);
  restart();
}

// ---------------------------------------------------------------- player

const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");
export const now = signal(0);
export const playing = signal(false);
export const speed = signal(1);
let raf = 0;
let last = 0;

export function restart() {
  const tr = trace.value;
  if (!tr) return;
  if (reduceMotion.matches) {
    now.value = tr.end;
    playing.value = false;
    return;
  }
  now.value = tr.minT;
  play();
}

export function play() {
  const tr = trace.value;
  if (!tr) return;
  if (now.value >= tr.end) now.value = tr.minT;
  playing.value = true;
  cancelAnimationFrame(raf);
  last = performance.now();
  const tick = (/** @type {number} */ ts) => {
    const t = trace.value;
    if (!t || !playing.value) return;
    now.value = Math.min(t.end, now.value + (ts - last) * speed.value);
    last = ts;
    if (now.value >= t.end) playing.value = false;
    else raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
}

export function pause() {
  playing.value = false;
  cancelAnimationFrame(raf);
}

export function skipToEnd() {
  pause();
  if (trace.value) now.value = trace.value.end;
}

export function seekTo(/** @type {number} */ t) {
  pause();
  now.value = t;
}

export const done = computed(() => !!trace.value && now.value >= trace.value.end);
export const seen = computed(() => trace.value?.events.filter((/** @type {any} */ e) => e.t <= now.value) ?? []);
export const current = computed(() => {
  const s = seen.value.filter((/** @type {any} */ e) => e.kind !== "node_started");
  return s[s.length - 1] ?? null;
});

// ---------------------------------------------------------------- words

const SOURCE = { statuspage: "Statuspage", chaos: "Cluster Sentinel" };
const OUTCOME = {
  completion: "replied",
  RateLimited: "was rate-limited",
  Unavailable: "was unavailable",
  Rejected: "rejected the call",
  MalformedResponse: "answered with something unreadable",
};
export const fmtMs = (/** @type {number} */ ms) =>
  Math.abs(ms) >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)} s` : `${Math.round(ms)} ms`;
export const src = (/** @type {string} */ id) => /** @type {any} */ (SOURCE)[id] ?? id;

/** One plain sentence for a trace event, composed only from its fields and earlier events. */
export function describe(/** @type {any} */ e, /** @type {any[]} */ events = []) {
  switch (e.kind) {
    case "scenario_setup":
      return e.step === "seed_cache"
        ? "Setup: a similar question's answer is placed in this run's private cache."
        : `Setup: all ${e.held} run slots are already taken.`;
    case "trace_started":
      return "Request received.";
    case "cache_lookup":
      if (e.hit) return `Cache hit: similarity ${e.similarity.toFixed(2)} clears the ${e.threshold.toFixed(2)} bar.`;
      return `${e.redis_refused ? "Redis refused the connection, so the in-memory cache answers: " : "Cache "}miss (${e.entries} ${e.entries === 1 ? "entry" : "entries"}).`;
    case "run_slot":
      return e.acquired
        ? `Got a run slot${e.waited_ms > 1 ? ` after ${fmtMs(e.waited_ms)}` : ""}.`
        : `No run slot freed up in ${fmtMs(e.waited_ms)}.`;
    case "node_started":
      return `${e.node} started.`;
    case "node_finished":
      return e.cancelled ? `${e.node} was cut off after ${fmtMs(e.duration_ms)}.` : `${e.node} finished in ${fmtMs(e.duration_ms)}.`;
    case "route_decided":
      return `${e.from} chose “${e.route}”: ${reason(e, events)}.`;
    case "model_call": {
      const who = `${e.role === "primary" ? "Primary" : "Fallback"} model ${e.model}`;
      const tail = e.outcome === "completion" ? ` in ${fmtMs(e.duration_ms)}` : "";
      return `${who} ${/** @type {any} */ (OUTCOME)[e.outcome] ?? e.outcome}${tail}${e.cooldown ? " and is now cooling down" : ""}.`;
    }
    case "telemetry_read": {
      const name = src(e.source);
      const pre = e.phase === "setup" ? "Setup: " : "";
      if (e.outcome === "read") return `${pre}${name} read, ${e.signals} signal${e.signals === 1 ? "" : "s"}.`;
      if (e.breaker_before === "open") return `${pre}${name} not called: its breaker is open.`;
      if (e.breaker_after === "open") return `${pre}${name} failed, and that failure tripped its breaker.`;
      return `${pre}${name} failed; its breaker stays closed.`;
    }
    case "validation_failed":
      return `The report broke the contract (${e.errors} error${e.errors === 1 ? "" : "s"}: ${e.fields.join(", ")}).`;
    case "reconciled":
      return e.changes.length
        ? `reconcile corrected ${e.changes.map((/** @type {any} */ c) => c.field.replace(/^recommended_actions\[(\d+)\]\./, "action $1 ")).join(", ")}.`
        : "reconcile confirmed the report's system facts.";
    case "deadline_exceeded":
      return `The ${e.deadline_s} s run deadline passed.`;
    case "cache_store":
      return "The report was stored in the cache.";
    case "trace_finished":
      return { cache_hit: "Answered from the cache.", analyzed: "Answered by a run.", shed: "Shed: told to retry later." }[
        /** @type {"cache_hit"|"analyzed"|"shed"} */ (e.triage_outcome)
      ];
    default:
      return e.kind;
  }
}

function reason(/** @type {any} */ e, /** @type {any[]} */ events) {
  const before = events.filter((x) => x.seq < e.seq);
  const lastOf = (/** @type {string} */ kind, /** @type {(x:any)=>boolean} */ f = () => true) =>
    [...before].reverse().find((x) => x.kind === kind && f(x));
  switch (`${e.from}:${e.route}`) {
    case "collect_telemetry:compensate": {
      const open = before.filter((x) => x.kind === "telemetry_read" && x.phase === "run" && x.breaker_after === "open");
      return `breaker open for ${open.map((x) => src(x.source)).join(" and ")}`;
    }
    case "collect_telemetry:synthesize":
      return "no targeted breaker is open";
    case "generate_report:validate":
      return "a model replied";
    case "generate_report:fallback":
      return "no model could answer";
    case "generate_report:finalize":
      return "no model answered the refine round, so the earlier report stands";
    case "validate_report:finalize": {
      const v = lastOf("node_finished", (x) => x.node === "validate_report");
      return v?.confidence != null ? `valid, confidence ${v.confidence.toFixed(2)}` : "valid";
    }
    case "validate_report:refine": {
      const v = lastOf("node_finished", (x) => x.node === "validate_report");
      return `valid, but confidence ${v?.confidence?.toFixed(2)} is under 0.60`;
    }
    case "validate_report:repair":
      return "the reply broke the contract and retries remain";
    case "validate_report:fallback":
      return "self-repair retries are used up";
    default:
      return "";
  }
}

/** The one-line trace summary, composed from the whole trace. */
export function summarize(/** @type {any} */ tr) {
  const ev = tr.events;
  const kinds = (/** @type {string} */ k) => ev.filter((/** @type {any} */ e) => e.kind === k);
  const out = tr.finished.triage_outcome;
  const parts = [];
  const head =
    out === "cache_hit"
      ? `Answered from the cache in ${fmtMs(tr.end)}`
      : out === "shed"
        ? `Shed after ${fmtMs(tr.end)}`
        : `Answered by a run in ${fmtMs(tr.end)}`;
  const calls = kinds("model_call");
  if (calls.length) {
    const fb = calls.filter((/** @type {any} */ c) => c.role === "fallback");
    const ok = calls.filter((/** @type {any} */ c) => c.outcome === "completion");
    if (fb.length && fb.some((/** @type {any} */ c) => c.outcome === "completion")) parts.push("failed over to the fallback model");
    if (!ok.length) parts.push("no model answered");
    else if (calls.length > 1 && !fb.length) parts.push(`${ok.length} model replies`);
  }
  const repairs = ev.filter((/** @type {any} */ e) => e.kind === "node_started" && e.node === "self_repair").length;
  if (repairs) parts.push(`${repairs} self-repair round${repairs > 1 ? "s" : ""}`);
  if (ev.some((/** @type {any} */ e) => e.kind === "node_started" && e.node === "refine_reflection")) parts.push("one refine round");
  for (const r of kinds("telemetry_read").filter((/** @type {any} */ r) => r.phase === "run" && r.outcome !== "read"))
    parts.push(`${src(r.source)} ${r.outcome === "tripped" ? "tripped" : "unavailable"}`);
  if (ev.some((/** @type {any} */ e) => e.kind === "node_started" && e.node === "fallback_report")) parts.push("synthesis fallback");
  if (kinds("deadline_exceeded").length) parts.push("run deadline hit, synthesis fallback");
  const rec = kinds("reconciled")[0];
  if (rec?.changes.length) parts.push(`reconcile changed ${rec.changes.length} fact${rec.changes.length > 1 ? "s" : ""}`);
  if (out === "analyzed" && !kinds("cache_store").length) parts.push("not cached");
  return { head, parts };
}

// ---------------------------------------------------------------- graph

const W = 150, WS = 136, H = 32;
/** Hand-placed layout (decision: no elkjs). x is the centre. */
export const LAYOUT = {
  analyze_query: [250, 40, W],
  collect_telemetry: [250, 100, W],
  compensate_blindspots: [420, 160, WS],
  degraded_fallback: [420, 215, WS],
  synthesize_telemetry: [250, 270, W],
  generate_report: [250, 340, W],
  refine_reflection: [90, 300, 128],
  self_repair: [90, 480, 128],
  validate_report: [250, 420, W],
  fallback_report: [420, 480, WS],
  finalize_report: [250, 550, W],
  __start__: [250, 6, 0],
  __end__: [250, 590, 0],
};
/** Edge ports and control points; key is "from>to". */
const EDGE = {
  "__start__>analyze_query": ["b", "t"],
  "analyze_query>collect_telemetry": ["b", "t"],
  "collect_telemetry>compensate_blindspots": ["r", "t"],
  "collect_telemetry>synthesize_telemetry": ["b", "t"],
  "compensate_blindspots>degraded_fallback": ["b", "t"],
  "degraded_fallback>synthesize_telemetry": ["b", "r"],
  "synthesize_telemetry>generate_report": ["b", "t"],
  "generate_report>validate_report": ["b", "t"],
  "generate_report>fallback_report": ["r", "t"],
  "generate_report>finalize_report": ["r", "r", 600],
  "validate_report>finalize_report": ["b", "t"],
  "validate_report>refine_reflection": ["l", "b"],
  "validate_report>self_repair": ["l", "t"],
  "validate_report>fallback_report": ["r", "l"],
  "refine_reflection>generate_report": ["r", "l"],
  "self_repair>generate_report": ["l", "l", -30],
  "fallback_report>finalize_report": ["b", "r"],
  "finalize_report>__end__": ["b", "t"],
};
const LABEL_AT = {
  "generate_report>finalize_report": [536, 448],
  "self_repair>generate_report": [16, 410],
};

function port(/** @type {string} */ n, /** @type {string} */ p) {
  const [x, y, w] = /** @type {any} */ (LAYOUT)[n];
  if (p === "t") return [x, y - (w ? H / 2 : 0)];
  if (p === "b") return [x, y + (w ? H / 2 : 0)];
  if (p === "l") return [x - w / 2, y];
  return [x + w / 2, y];
}

function edgePath(/** @type {string} */ key) {
  const [a, b] = key.split(">");
  const [pa, pb, lane] = /** @type {any} */ (EDGE)[key];
  const [x1, y1] = port(a, pa);
  const [x2, y2] = port(b, pb);
  if (lane != null) return `M${x1},${y1} C${lane},${y1} ${lane},${y2} ${x2},${y2}`;
  const v = (/** @type {string} */ p) => p === "t" || p === "b";
  const c1 = v(pa) ? [x1, (y1 + y2) / 2] : [(x1 + x2) / 2, y1];
  const c2 = v(pb) ? [x2, (y1 + y2) / 2] : [(x1 + x2) / 2, y2];
  if (v(pa) && v(pb) && x1 === x2) return `M${x1},${y1} L${x2},${y2}`;
  return `M${x1},${y1} C${c1[0]},${c1[1]} ${c2[0]},${c2[1]} ${x2},${y2}`;
}

/** Principle 1: every get_graph() node and edge must be placed. Shown on the page if not. */
export const layoutGaps = signal(/** @type {string[]} */ ([]));
function checkLayout() {
  const g = data.value.graph;
  const gaps = [
    ...g.nodes.filter((/** @type {string} */ n) => !(n in LAYOUT)).map((/** @type {string} */ n) => `node ${n}`),
    ...g.edges.filter((/** @type {any} */ e) => !(`${e.from}>${e.to}` in EDGE)).map((/** @type {any} */ e) => `edge ${e.from}→${e.to}`),
    ...Object.keys(EDGE).filter((k) => !g.edges.some((/** @type {any} */ e) => `${e.from}>${e.to}` === k)).map((k) => `stale edge ${k}`),
  ];
  layoutGaps.value = gaps;
}

const graphState = computed(() => {
  const s = seen.value;
  const started = s.filter((/** @type {any} */ e) => e.kind === "node_started");
  const finished = s.filter((/** @type {any} */ e) => e.kind === "node_finished");
  const visits = /** @type {Record<string, number>} */ ({});
  for (const e of started) visits[e.node] = (visits[e.node] ?? 0) + 1;
  const active = started.length > finished.length ? started[started.length - 1].node : null;
  const failed = new Set(finished.filter((/** @type {any} */ e) => e.cancelled).map((/** @type {any} */ e) => e.node));
  const edges = /** @type {Record<string, number>} */ ({});
  const path = ["__start__", ...started.map((/** @type {any} */ e) => e.node)];
  if (done.value && started.length && !failed.size) path.push("__end__");
  for (let i = 1; i < path.length; i++) {
    const k = `${path[i - 1]}>${path[i]}`;
    edges[k] = (edges[k] ?? 0) + 1;
  }
  return { visits, active, edges, failed, ran: started.length > 0 };
});

export function Graph({ onNode = /** @type {any} */ (null), compact = false }) {
  const d = data.value;
  if (!d) return null;
  const g = graphState.value;
  const tr = trace.value;
  const noRun = done.value && !g.ran;
  return html`
    <figure class="graph ${compact ? "graph--compact" : ""}">
      <div class="graph__scroll">
        <svg viewBox="-40 -6 680 610" role="img" aria-labelledby="graph-title">
          <title id="graph-title">The triage graph from get_graph(). Lit nodes ran in this trace${
            Object.keys(g.visits).length ? `: ${Object.keys(g.visits).join(", ")}` : ""
          }.</title>
          <defs>
            <marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L8,4 L0,8 z" class="arrowhead" />
            </marker>
            <marker id="arr-on" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L8,4 L0,8 z" class="arrowhead arrowhead--on" />
            </marker>
          </defs>
          ${d.graph.edges.map((/** @type {any} */ e) => {
            const k = `${e.from}>${e.to}`;
            if (!(k in EDGE)) return null;
            const on = g.edges[k];
            const p = edgePath(k);
            const [lx, ly] = /** @type {any} */ (LABEL_AT)[k] ?? midpoint(k);
            return html`<g class="edge ${on ? "edge--on" : ""} ${e.conditional ? "edge--cond" : ""}">
              <path d=${p} marker-end=${on ? "url(#arr-on)" : "url(#arr)"} />
              ${e.label && html`<text x=${lx} y=${ly} class="edge__label">${e.label}${on > 1 ? ` ×${on}` : ""}</text>`}
            </g>`;
          })}
          ${Object.entries(LAYOUT).map(([n, [x, y, w]]) => {
            if (!w) return html`<circle cx=${x} cy=${y} r="4" class="terminal ${g.ran ? "terminal--on" : ""}" />`;
            const v = g.visits[n];
            const cls = [
              "node",
              v ? "node--on" : "",
              g.active === n ? "node--active" : "",
              g.failed.has(n) ? "node--cut" : "",
            ].join(" ");
            const label = html`
              <rect x=${x - w / 2} y=${y - H / 2} width=${w} height=${H} rx="6" />
              <text x=${x} y=${y + 4} text-anchor="middle">${n}</text>
              ${v > 1 && html`<text x=${x + w / 2 - 4} y=${y - H / 2 - 4} text-anchor="end" class="node__count">×${v}</text>`}`;
            return onNode
              ? html`<g class=${cls} tabindex="0" role="button" aria-label="${n}${v ? `, ran ${v} time${v > 1 ? "s" : ""}` : ", did not run"}"
                  onClick=${() => onNode(n)} onKeyDown=${(/** @type {KeyboardEvent} */ ev) => (ev.key === "Enter" || ev.key === " ") && (ev.preventDefault(), onNode(n))}>${label}</g>`
              : html`<g class=${cls}>${label}</g>`;
          })}
        </svg>
      </div>
      ${noRun && html`<figcaption class="graph__note">No run: ${tr?.finished.triage_outcome === "shed" ? "the request was shed before a slot freed up" : "the cache answered first"}.</figcaption>`}
      ${layoutGaps.value.length > 0 && html`<p class="graph__gaps" role="alert">Layout is missing ${layoutGaps.value.join(", ")}.</p>`}
    </figure>`;
}

function midpoint(/** @type {string} */ k) {
  const [a, b] = k.split(">");
  const [pa, pb] = /** @type {any} */ (EDGE)[k];
  const [x1, y1] = port(a, pa);
  const [x2, y2] = port(b, pb);
  return [(x1 + x2) / 2 + 6, (y1 + y2) / 2 + 3];
}

// ---------------------------------------------------------------- waterfall

const CHILD = new Set(["model_call", "telemetry_read", "cache_lookup"]);

/** Rows: setup, gateway work, and each node span with the calls made inside it. */
export const rows = computed(() => {
  const tr = trace.value;
  if (!tr) return [];
  const ev = tr.events;
  const nodes = ev.filter((/** @type {any} */ e) => e.kind === "node_finished");
  const inside = (/** @type {any} */ e) =>
    nodes.find((/** @type {any} */ n) => e.s >= n.s - 0.5 && e.s <= n.s + n.duration_ms + 0.5 && e.t_ms <= n.t_ms + 0.5);
  /** @type {any[]} */
  const out = [];
  for (const e of ev) {
    if (e.kind === "telemetry_read" && e.phase === "setup") out.push({ e, label: `setup · ${src(e.source)}`, lane: "setup", dur: e.duration_ms });
    else if (e.kind === "scenario_setup") out.push({ e, label: `setup · ${e.step.replace("_", " ")}`, lane: "setup", dur: 0 });
    else if (e.kind === "cache_lookup" && !inside(e)) out.push({ e, label: "cache lookup", lane: "gateway", dur: e.duration_ms });
    else if (e.kind === "run_slot") out.push({ e, label: "run slot", lane: e.acquired ? "gateway" : "bad", dur: e.waited_ms });
    else if (e.kind === "node_finished") {
      out.push({ e, label: e.node, lane: e.cancelled ? "bad" : "node", dur: e.duration_ms });
      for (const c of ev.filter((/** @type {any} */ c) => CHILD.has(c.kind) && c.phase !== "setup" && inside(c) === e)) {
        const label =
          c.kind === "model_call" ? `${c.role} · ${c.model}` : c.kind === "telemetry_read" ? src(c.source) : "history lookup";
        const lane =
          c.kind === "model_call"
            ? c.outcome === "completion" ? "model" : "bad"
            : c.kind === "telemetry_read" ? (c.outcome === "read" ? "telemetry" : "bad") : "gateway";
        out.push({ e: c, label, lane, dur: c.duration_ms, child: true });
      }
    } else if (e.kind === "cache_store") out.push({ e, label: "cache store", lane: "gateway", dur: e.duration_ms });
    else if (e.kind === "deadline_exceeded") out.push({ e: { ...e, s: e.t }, label: "run deadline", lane: "bad", dur: 0 });
  }
  return out;
});

export function Waterfall({ onSelect = /** @type {any} */ (null), dense = false }) {
  const tr = trace.value;
  if (!tr) return null;
  const span = tr.end - tr.minT || 1;
  const pct = (/** @type {number} */ t) => ((t - tr.minT) / span) * 100;
  const ticks = niceTicks(tr.minT, tr.end);
  const sel = selectedSeq.value;
  return html`
    <div class="wf ${dense ? "wf--dense" : ""}" role="table" aria-label="Timing of every step">
      <div class="wf__axis" role="row" aria-hidden="true">
        <span class="wf__name"></span>
        <span class="wf__track">
          ${ticks.map((t) => html`<span class="wf__tick" style="left:${pct(t)}%">${t === 0 ? "0" : fmtMs(t)}</span>`)}
        </span>
      </div>
      ${rows.value.map((r) => {
        const visible = r.e.s <= now.value;
        const w = Math.max(0, Math.min(r.dur, now.value - r.e.s));
        return html`
          <div class="wf__row ${r.child ? "wf__row--child" : ""} ${visible ? "" : "wf__row--pending"}" role="row">
            <span class="wf__name" role="cell">${r.label}</span>
            <span class="wf__track" role="cell">
              ${tr.minT < 0 && html`<span class="wf__zero" style="left:${pct(0)}%"></span>`}
              <button type="button"
                class="wf__bar wf__bar--${r.lane} ${sel === r.e.seq ? "is-selected" : ""}"
                style="left:${pct(r.e.s)}%;width:max(3px, ${(w / span) * 100}%)"
                aria-label="${r.label}: ${describe(r.e, tr.events)}"
                aria-pressed=${sel === r.e.seq}
                disabled=${!visible}
                onClick=${() => { selectedSeq.value = r.e.seq; onSelect?.(r.e); }}></button>
              ${visible && r.dur >= 1 && html`<span class="wf__dur" style="left:calc(${pct(r.e.s + w)}% + 6px)">${fmtMs(r.dur)}</span>`}
            </span>
          </div>`;
      })}
      <div class="wf__playhead" aria-hidden="true" style="left:calc(var(--wf-name) + (100% - var(--wf-name)) * ${pct(now.value) / 100})"></div>
    </div>`;
}

function niceTicks(/** @type {number} */ a, /** @type {number} */ b) {
  const span = b - a;
  const step = [50, 100, 250, 500, 1000, 2000].find((s) => span / s <= 4) ?? 5000;
  const out = [];
  for (let t = Math.ceil(a / step) * step; t <= b; t += step) out.push(t);
  return out;
}

// ---------------------------------------------------------------- summary, log, report

export function SummaryLine() {
  const tr = trace.value;
  if (!tr) return null;
  if (!done.value) return html`<p class="summary summary--live" aria-live="polite">${current.value ? describe(current.value, tr.events) : "Starting…"}</p>`;
  const { head, parts } = summarize(tr);
  return html`<p class="summary" aria-live="polite"><strong>${head}</strong>${parts.map((p) => html`<span class="summary__part">${p}</span>`)}</p>`;
}

export function EventLog({ upTo = true }) {
  const tr = trace.value;
  if (!tr) return null;
  const list = upTo ? seen.value : tr.events;
  return html`
    <ol class="log">
      ${list.filter((/** @type {any} */ e) => e.kind !== "node_started").map((/** @type {any} */ e) => html`
        <li class="log__item log__item--${e.kind} ${selectedSeq.value === e.seq ? "is-selected" : ""}">
          <button type="button" class="log__btn" onClick=${() => (selectedSeq.value = e.seq)}>
            <span class="log__t">${e.t < 0 ? "setup" : fmtMs(e.t)}</span>
            <span class="log__kind">${e.kind}</span>
            <span class="log__text">${describe(e, tr.events)}</span>
          </button>
        </li>`)}
    </ol>`;
}

/** The raw DTO of the selected event, as it would arrive over SSE. */
export function EventDetail() {
  const tr = trace.value;
  const e = tr?.events.find((/** @type {any} */ x) => x.seq === selectedSeq.value);
  if (!e) return html`<p class="muted">Select a bar or a log line to see the event exactly as it arrives over the stream.</p>`;
  const { t, s, response, ...dto } = e;
  return html`
    <div class="detail">
      <p class="detail__sentence">${describe(e, tr.events)}</p>
      <pre class="detail__json" tabindex="0"><code>${`event: ${e.kind}\ndata: ${JSON.stringify(response ? { ...dto, response: "…" } : dto, null, 1)}`}</code></pre>
    </div>`;
}

export function Report() {
  const tr = trace.value;
  if (!tr || !done.value) return null;
  const r = tr.response?.report;
  if (!r) return html`<div class="report report--empty"><p><strong>No report.</strong> A shed request gets a retry-after (${tr.response?.retry_after_s} s) instead, so the caller is never left hanging.</p></div>`;
  const rec = tr.events.find((/** @type {any} */ e) => e.kind === "reconciled");
  const changed = new Set((rec?.changes ?? []).map((/** @type {any} */ c) => c.field));
  const fellBack = tr.events.some((/** @type {any} */ e) => (e.kind === "node_started" && e.node === "fallback_report") || e.kind === "deadline_exceeded");
  const status = r.degradation_status;
  return html`
    <article class="report" aria-label="Triage report">
      <header class="report__head">
        <span class="chip chip--sev-${r.severity.toLowerCase()}">${r.severity}</span>
        <span class="chip chip--${status === "HEALTHY" ? "ok" : status === "PARTIALLY_DEGRADED" ? "warn" : "bad"} ${changed.has("degradation_status") ? "is-reconciled" : ""}"
          title=${changed.has("degradation_status") ? "Set by reconcile" : ""}>${status}</span>
        <span class="report__conf">confidence <b>${r.confidence_score.toFixed(2)}</b></span>
        ${tr.response.similarity != null && html`<span class="report__conf">from cache, similarity <b>${tr.response.similarity.toFixed(2)}</b></span>`}
      </header>
      <h3 class="report__summary">${r.summary}</h3>
      <p class="report__provenance">${fellBack
        ? "Built deterministically by fallback_report: no model text."
        : "Summary, analysis and actions are the scripted model reply. Status, breakers and approvals are system facts set by reconcile."}</p>
      <dl class="report__facts">
        <dt>Root cause</dt><dd>${r.root_cause_analysis}</dd>
        <dt>Breakers tripped</dt><dd class=${changed.has("circuit_breakers_tripped") ? "is-reconciled" : ""}>${r.circuit_breakers_tripped.length ? r.circuit_breakers_tripped.map(src).join(", ") : "none"}</dd>
      </dl>
      ${r.recommended_actions.length > 0 && html`
        <div class="report__actions-wrap">
          <table class="report__actions">
            <thead><tr><th scope="col">Priority</th><th scope="col">Action</th><th scope="col">Type</th><th scope="col">Approval</th></tr></thead>
            <tbody>${r.recommended_actions.map((/** @type {any} */ a, /** @type {number} */ i) => html`
              <tr>
                <td><span class="mono">${a.priority}</span></td>
                <td>${a.action}<div class="muted small">${a.target_system}</div></td>
                <td>${a.action_type}</td>
                <td class=${changed.has(`recommended_actions[${i}].requires_approval`) ? "is-reconciled" : ""}>
                  ${a.requires_approval ? "Required" : "Not needed"}
                </td>
              </tr>`)}
            </tbody>
          </table>
        </div>`}
      ${changed.size > 0 && html`<p class="report__legend"><span class="is-reconciled">Marked</span> values were corrected by reconcile.</p>`}
    </article>`;
}

// ---------------------------------------------------------------- controls

export function Transport({ scrub = false }) {
  const tr = trace.value;
  if (!tr) return null;
  return html`
    <div class="transport">
      ${playing.value
        ? html`<button type="button" class="btn" onClick=${pause}>Pause</button>`
        : html`<button type="button" class="btn btn--primary" onClick=${done.value ? restart : play}>${done.value ? "Replay" : "Play"}</button>`}
      <button type="button" class="btn btn--quiet" onClick=${skipToEnd} disabled=${done.value}>Skip to end</button>
      ${scrub && html`
        <label class="transport__scrub">
          <span class="visually-hidden">Position in trace</span>
          <input type="range" id="scrub" min=${tr.minT} max=${tr.end} step="1" value=${now.value}
            onInput=${(/** @type {any} */ ev) => seekTo(Number(ev.currentTarget.value))} />
        </label>
        <span class="transport__time mono">${fmtMs(Math.max(0, now.value))} / ${fmtMs(tr.end)}</span>`}
    </div>`;
}

export function SetupNotes() {
  const sc = scenario.value;
  if (!sc?.setup.length) return null;
  return html`<ul class="setup" aria-label="Scenario setup">${sc.setup.map((/** @type {string} */ s) => html`<li>${s}</li>`)}</ul>`;
}

export function ScriptedBadge() {
  return html`<span class="badge" title="Scenario runs script the model replies and Statuspage. The graph, gateway, breakers, reconciliation and cache code are real.">Model replies scripted</span>`;
}

export const theme = signal(/** @type {"system"|"light"|"dark"} */ ("system"));
effect(() => {
  if (theme.value === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme.value);
});
export function ThemeToggle() {
  const next = { system: "light", light: "dark", dark: "system" };
  return html`<button type="button" class="btn btn--quiet btn--sm" onClick=${() => (theme.value = /** @type {any} */ (next)[theme.value])}>
    Theme: ${theme.value}</button>`;
}

/** Scroll a container's selected item into view when the selection changes. */
export function useScrollSelected(/** @type {any} */ dep) {
  const ref = useRef(/** @type {HTMLElement|null} */ (null));
  useEffect(() => {
    ref.current?.querySelector(".is-selected")?.scrollIntoView({ block: "nearest" });
  }, [dep]);
  return ref;
}
