// @ts-check
// The live-trace stage (#118): one run's headline, its graph above its waterfall. Dumb by
// design: it draws what trace-reducer.mjs derives and calls back for Replay and Retry.
// `data-run-state` on the stage is the run's status; browser tests wait on it.
import { html } from "htm/preact";
import { graphView, runHeadline } from "../trace-reducer.mjs";
import { Graph } from "./graph.mjs";
import { Waterfall } from "./waterfall.mjs";

/** @typedef {import("../trace-reducer.mjs").RunView} RunView */
/** @typedef {import("../trace-reducer.mjs").GraphLayout} GraphLayout */

/**
 * @param {{
 *   run: RunView,
 *   layout: GraphLayout | "loading" | "failed",
 *   onReplay: () => void,
 *   onSelectNode?: (name: string) => void,
 * }} props
 */
export function Stage({ run, layout, onReplay, onSelectNode }) {
  const headline = runHeadline(run);
  return html`
    <div class="run" data-run-state=${run.status} data-dimmed=${String(headline.dimmed)}>
      <div class="run__head" role="status">
        ${headline.line
          ? html`<p class="run__line" data-run-line>${headline.line}</p>`
          : html`<p class="run__line run__line--idle">
              Pick a scenario and its path through the triage graph lights up here, node by node.
            </p>`}
        ${run.trace.scripted &&
        html`<span class="badge" data-run-badge>Model replies scripted</span>`}
        ${headline.action &&
        html`<button class="button" type="button" onClick=${onReplay}>${headline.action}</button>`}
      </div>
      <div class="run__panels">
        ${layout === "loading"
          ? null
          : layout === "failed"
            ? html`<p class="notice">The graph's layout didn't load. The timeline below still shows the run.</p>`
            : html`<${Graph} view=${graphView(layout, run.trace)} onSelectNode=${onSelectNode} />`}
        <${Waterfall} trace=${run.trace} />
      </div>
    </div>
  `;
}
