// @ts-check
// Page shell (ADR 0010): renders the dynamic regions of index.html with their empty states.
// The page's prose is static HTML; this module only fills the mount points. No logic lives
// here: the trace-to-view reducer is the one logic module (CODING_STANDARDS.md, Frontend).
import { signal } from "@preact/signals";
import { html } from "htm/preact";
import { render } from "preact";

/** @typedef {import("@preact/signals").Signal<null>} RunSignal */

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

/**
 * The live-trace column: graph above waterfall, empty until a run plays.
 * @param {{ run: RunSignal }} props
 */
function Stage({ run }) {
  if (run.value !== null) return null;
  return html`
    <div class="stage__panels">
      <${EmptyState} title="Graph">
        Pick a scenario and its path through the triage graph lights up here, node by node.
      <//>
      <${EmptyState} title="Timeline">
        Each node, model call and telemetry read gets a bar on one shared clock.
      <//>
    </div>
  `;
}

function Chapters() {
  return html`
    <${EmptyState} title="No scenarios yet">
      The scenarios appear here in four chapters, each opening inline to play.
    <//>
  `;
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

function main() {
  /** @type {RunSignal} */
  const run = signal(null);
  mount("stage-root", html`<${Stage} run=${run} />`);
  mount("chapters-root", html`<${Chapters} />`);
  mount("free-play-root", html`<${FreePlay} />`);
  // Browser tests wait on this instead of sleeping: every region has rendered.
  document.documentElement.dataset.shell = "ready";
}

main();
