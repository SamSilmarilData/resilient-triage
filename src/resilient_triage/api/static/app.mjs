// @ts-check
// Page shell (ADR 0010): renders the dynamic regions of index.html. The page's prose is static
// HTML; this module only fills the mount points and wires signals to components. No logic
// lives here: the trace-to-view reducer is the one logic module (CODING_STANDARDS.md, Frontend).
// Importing @preact/signals installs its Preact integration, so components re-render when a
// signal they read changes.
import { signal } from "@preact/signals";
import { html } from "htm/preact";
import { render } from "preact";
import { FreePlay } from "./components/free-play.mjs";
import { Stage } from "./components/stage.mjs";
import { freePlaySource } from "./sse.mjs";
import { createRunController, idleRun } from "./trace-reducer.mjs";

/** @typedef {import("./trace-reducer.mjs").GraphLayout} GraphLayout */

/** The stage's current run, as the run controller last reported it. */
const run = signal(idleRun());
/** graph-layout.json once fetched. */
const layout = signal(/** @type {GraphLayout | "loading" | "failed"} */ ("loading"));

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

/** The free-play panel's own run, so its errors and lost streams stay in that panel (#101). */
const freePlayRun = signal(idleRun());

/** The free-play panel's run controller: a visitor's question plays here, not on the stage. */
export const freePlay = createRunController({
  onChange: (view) => {
    freePlayRun.value = view;
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

function Chapters() {
  return html`
    <${EmptyState} title="No scenarios yet">
      The scenarios appear here in four chapters, each opening inline to play.
    <//>
  `;
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
  mount("stage-root", html`<${LiveStage} />`);
  mount("chapters-root", html`<${Chapters} />`);
  mount("free-play-root", html`<${LiveFreePlay} />`);
  // Browser tests wait on this instead of sleeping: every region has rendered.
  document.documentElement.dataset.shell = "ready";
}

main();
