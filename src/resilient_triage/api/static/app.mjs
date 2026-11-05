// @ts-check
// Page shell (ADR 0010): renders the dynamic regions of index.html. The page's prose is static
// HTML; this module only fills the mount points and wires signals to components. No logic
// lives here: the trace-to-view reducer is the one logic module (CODING_STANDARDS.md, Frontend).
// Importing @preact/signals installs its Preact integration, so components re-render when a
// signal they read changes.
import { signal } from "@preact/signals";
import { html } from "htm/preact";
import { render } from "preact";
import { Chapters } from "./components/chapter.mjs";
import { FreePlay } from "./components/free-play.mjs";
import { NodeList } from "./components/how-its-built.mjs";
import { NodeInspector } from "./components/node-inspector.mjs";
import { Stage } from "./components/stage.mjs";
import { freePlaySource, scenarioSource } from "./sse.mjs";
import {
  createRunController,
  idleRun,
  inspectorView,
  nextScenario,
  scenarioRun,
} from "./trace-reducer.mjs";

/** @typedef {import("./trace-reducer.mjs").GraphLayout} GraphLayout */
/** @typedef {import("./trace-reducer.mjs").Catalogue} Catalogue */
/** @typedef {import("./trace-reducer.mjs").GraphNode} GraphNode */

/** The stage's current run, as the run controller last reported it. */
const run = signal(idleRun());
/** graph-layout.json once fetched. */
const layout = signal(/** @type {GraphLayout | "loading" | "failed"} */ ("loading"));
/** GET /scenarios once fetched: the chapters' copy. */
const catalogue = signal(/** @type {Catalogue | "loading" | "failed"} */ ("loading"));
/** The scenario opened inline, if any; one at a time. */
const openId = signal(/** @type {string | null} */ (null));
/** The scenario last played on the stage, if any. */
const playedId = signal(/** @type {string | null} */ (null));

/**
 * The stage's run controller: the guided scenarios play here (free play has its own, below).
 * A run starts with `stage.play(source)`, a source from sse.mjs; browser tests import it from
 * this module the same way.
 */
export const stage = createRunController({
  onChange: (view) => {
    run.value = view;
  },
});

/** The free-play panel's own run, so its errors and lost streams stay in that panel (#101). */
const freePlayRun = signal(idleRun());

/** The free-play panel's run controller: a visitor's question plays here, not on the stage. */
export const freePlay = createRunController({
  onChange: (view) => {
    freePlayRun.value = view;
  },
});

/** The live-trace column, bound to the stage's signals. */
function LiveStage() {
  return html`<${Stage}
    run=${run.value}
    layout=${layout.value}
    onReplay=${() => stage.replay()}
    onSelectNode=${inspect}
  />`;
}

/** The guided chapters, bound to the catalogue and the stage's run. */
function GuidedChapters() {
  const loaded = catalogue.value;
  return html`<${Chapters}
    catalogue=${loaded}
    onRetry=${loadCatalogue}
    openId=${openId.value}
    runOf=${(/** @type {string} */ id) => scenarioRun(run.value, playedId.value, id)}
    nextOf=${(/** @type {string} */ id) => (typeof loaded === "object" ? nextScenario(loaded, id) : null)}
    onToggle=${(/** @type {string} */ id) => {
      openId.value = openId.value === id ? null : id;
    }}
    onPlay=${(/** @type {string} */ id) => {
      playedId.value = id;
      stage.play(scenarioSource(id));
    }}
    onSkip=${(/** @type {string} */ id) => {
      const next = typeof loaded === "object" ? nextScenario(loaded, id) : null;
      if (!next) return;
      openId.value = next.id;
      // Keyboard users land on the scenario they skipped to.
      requestAnimationFrame(() => document.getElementById(`scenario-${next.id}-toggle`)?.focus());
    }}
  />`;
}

/** Fetch GET /scenarios into its signal; on failure the chapters offer Retry (#101). */
async function loadCatalogue() {
  catalogue.value = "loading";
  try {
    const response = await fetch("/scenarios");
    catalogue.value = response.ok ? await response.json() : "failed";
  } catch {
    catalogue.value = "failed";
  }
}

/** The free-play panel, bound to its own run and the shared graph layout. */
function LiveFreePlay() {
  return html`<${FreePlay}
    run=${freePlayRun.value}
    layout=${layout.value}
    onSubmit=${(request) => freePlay.play(freePlaySource(request))}
    onReplay=${() => freePlay.replay()}
  />`;
}

// The node inspector (#121): GET /graph/nodes, and the node it shows (null when closed).
// A graph node on the stage and a button in How it's built's node list both open it.
const graphNodes = signal(/** @type {GraphNode[] | "loading" | "failed"} */ ("loading"));
const inspected = signal(/** @type {string | null} */ (null));

/** @param {string} name */
function inspect(name) {
  inspected.value = name;
}

/** Fetch every node's inspector content into its signal. */
async function loadGraphNodes() {
  graphNodes.value = "loading";
  try {
    const response = await fetch("/graph/nodes");
    graphNodes.value = response.ok ? (await response.json()).nodes : "failed";
  } catch {
    graphNodes.value = "failed";
  }
}

function BuiltNodes() {
  return html`<${NodeList} nodes=${graphNodes.value} onSelect=${inspect} onRetry=${loadGraphNodes} />`;
}

function Inspector() {
  const close = () => {
    inspected.value = null;
  };
  const view = inspectorView(graphNodes.value, inspected.value);
  return html`<${NodeInspector} view=${view} onClose=${close} />`;
}

/**
 * Render a component into a mount point and mark the region as no longer busy.
 * @param {string} id
 * @param {any} view
 */
function mount(id, view) {
  const root = document.getElementById(id);
  if (!root) return;
  render(view, root);
  root.removeAttribute("aria-busy");
}

/** Fetch the graph's layout into its signal; the stage draws the graph once it lands. */
async function loadLayout() {
  try {
    const response = await fetch("/static/graph-layout.json");
    layout.value = response.ok ? await response.json() : "failed";
  } catch {
    layout.value = "failed";
  }
}

function main() {
  loadLayout();
  loadCatalogue();
  mount("stage-root", html`<${LiveStage} />`);
  mount("chapters-root", html`<${GuidedChapters} />`);
  mount("free-play-root", html`<${LiveFreePlay} />`);
  loadGraphNodes();
  mount("built-nodes-root", html`<${BuiltNodes} />`);
  mount("node-inspector-root", html`<${Inspector} />`);
  // Browser tests wait on this instead of sleeping: every region has rendered.
  document.documentElement.dataset.shell = "ready";
}

main();
