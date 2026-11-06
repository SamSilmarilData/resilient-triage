// @ts-check
// Page shell (ADR 0010): renders the dynamic regions of index.html. The page's prose is static
// HTML; this module only fills the mount points and wires signals to components. No logic
// lives here: the trace-to-view reducer is the one logic module (CODING_STANDARDS.md, Frontend),
// and sse.mjs does the network I/O.
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
import { freePlaySource, loadInto, scenarioSource } from "./sse.mjs";
import {
  createRunController,
  createScenarioStage,
  idleRun,
  inspectorView,
  needsReveal,
  stageRevealTop,
  toggledOpen,
} from "./trace-reducer.mjs";

/** @typedef {import("./trace-reducer.mjs").GraphLayout} GraphLayout */
/** @typedef {import("./trace-reducer.mjs").Catalogue} Catalogue */
/** @typedef {import("./trace-reducer.mjs").GraphNode} GraphNode */

/**
 * A signal holding a run, and the run controller that reports into it.
 * @returns {{ run: import("@preact/signals").Signal<import("./trace-reducer.mjs").RunView>,
 *             controller: ReturnType<typeof createRunController> }}
 */
function runSignal() {
  const run = signal(idleRun());
  const controller = createRunController({
    onChange: (view) => {
      run.value = view;
    },
  });
  return { run, controller };
}

/** graph-layout.json once fetched. */
const layout = signal(
  /** @type {import("./trace-reducer.mjs").Loadable<GraphLayout>} */ ("loading"),
);
/** GET /scenarios once fetched: the chapters' copy. */
const catalogue = signal(
  /** @type {import("./trace-reducer.mjs").Loadable<Catalogue>} */ ("loading"),
);
/** The scenario opened inline, if any; one at a time. */
const openId = signal(/** @type {string | null} */ (null));

/** The stage's run: the guided scenarios play here (free play has its own, below). */
const stageRun = runSignal();
/**
 * The stage's run controller. A run starts with `stage.play(source)`, a source from sse.mjs;
 * browser tests import it from this module the same way.
 */
export const stage = stageRun.controller;
/** The guided scenarios on the stage: Play and Skip to end by scenario id. */
const scenarios = createScenarioStage(stage, scenarioSource);

/** The free-play panel's own run, so its errors and lost streams stay in that panel (#101). */
const freePlayRun = runSignal();
/** The free-play panel's run controller: a visitor's question plays here, not on the stage. */
export const freePlay = freePlayRun.controller;

/** The live-trace column, bound to the stage's signals. */
function LiveStage() {
  return html`<${Stage}
    run=${stageRun.run.value}
    layout=${layout.value}
    onReplay=${() => stage.replay()}
    onSelectNode=${inspect}
  />`;
}

/** The guided chapters, bound to the catalogue and the stage's run. */
function GuidedChapters() {
  const run = stageRun.run.value;
  return html`<${Chapters}
    catalogue=${catalogue.value}
    onRetry=${loadCatalogue}
    openId=${openId.value}
    runOf=${(/** @type {string} */ id) => scenarios.runOf(run, id)}
    onToggle=${(/** @type {string} */ id) => {
      openId.value = toggledOpen(openId.value, id);
    }}
    onPlay=${(/** @type {string} */ id) => {
      scenarios.play(id);
      revealStage();
    }}
    onSkip=${(/** @type {string} */ id) => {
      // Skip to end: no scroll to the stage, since the report appears in the scenario itself.
      scenarios.skip(id);
    }}
  />`;
}

/**
 * Bring the stage into view for a run that just started, when needsReveal() says so. The
 * scroll follows html's scroll-behavior, so it is instant under reduced motion. Focus stays
 * on Play. A pinned stage scrolled down inside itself goes back to its graph (#157).
 */
function revealStage() {
  const element = document.querySelector(".stage");
  if (!element) return;
  if (needsReveal(element.getBoundingClientRect(), window.innerHeight)) {
    element.scrollIntoView({ block: "start" });
  }
  const top = stageRevealTop(element);
  if (top !== null) element.scrollTo({ top });
}

/** Fetch GET /scenarios into its signal; on failure the chapters offer Retry (#101). */
function loadCatalogue() {
  return loadInto(catalogue, "/scenarios");
}

/** The free-play panel, bound to its own run and the shared graph layout. */
function LiveFreePlay() {
  return html`<${FreePlay}
    run=${freePlayRun.run.value}
    layout=${layout.value}
    onSubmit=${(request) => freePlay.play(freePlaySource(request))}
    onReplay=${() => freePlay.replay()}
    onSelectNode=${inspect}
  />`;
}

// The node inspector (#121): GET /graph/nodes, and the node it shows (null when closed).
// A graph node (on the stage or in free play) and a button in How it's built's node list
// all open it.
const graphNodes = signal(
  /** @type {import("./trace-reducer.mjs").Loadable<GraphNode[]>} */ ("loading"),
);
const inspected = signal(/** @type {string | null} */ (null));

/** @param {string} name */
function inspect(name) {
  inspected.value = name;
}

/** Fetch every node's inspector content into its signal. */
function loadGraphNodes() {
  return loadInto(graphNodes, "/graph/nodes", (body) => body.nodes);
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
function loadLayout() {
  return loadInto(layout, "/static/graph-layout.json");
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
