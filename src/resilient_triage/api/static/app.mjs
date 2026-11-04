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
import { Stage } from "./components/stage.mjs";
import { scenarioSource } from "./sse.mjs";
import { createRunController, idleRun, nextScenario, scenarioRun } from "./trace-reducer.mjs";

/** @typedef {import("./trace-reducer.mjs").GraphLayout} GraphLayout */
/** @typedef {import("./trace-reducer.mjs").Catalogue} Catalogue */

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
 * The page's one run controller: whatever plays (a scenario, free play) plays on the stage.
 * Panels start a run with `stage.play(source)`, a source from sse.mjs; browser tests import
 * it from this module the same way.
 */
export const stage = createRunController({
  onChange: (view) => {
    run.value = view;
  },
});

/**
 * A quiet bordered panel that says what will appear in it.
 * @param {{ title: string, children: any }} props
 */
function EmptyState({ title, children }) {
  return html`
    <div class="empty">
      <p class="empty__title">${title}</p>
      <p class="empty__body">${children}</p>
    </div>
  `;
}

/** The live-trace column, bound to the stage's signals. */
function LiveStage() {
  return html`<${Stage} run=${run.value} layout=${layout.value} onReplay=${() => stage.replay()} />`;
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

function FreePlay() {
  return html`
    <${EmptyState} title="Free play isn't open yet">
      Your own question and its live trace will go here.
    <//>
  `;
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
  mount("free-play-root", html`<${FreePlay} />`);
  // Browser tests wait on this instead of sleeping: every region has rendered.
  document.documentElement.dataset.shell = "ready";
}

main();
