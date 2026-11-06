// @ts-check
// The live-trace stage (#118): one run's headline, its graph above its waterfall. Dumb by
// design: it draws what trace-reducer.mjs derives and calls back for Replay and Retry.
// `data-run-state` on the stage is the run's status; browser tests wait on it. Its one live
// region says only how a run ended (runAnnouncement), not the headline's running tick.
import { html } from "htm/preact";
import { graphView, runAnnouncement, runHeadline } from "../trace-reducer.mjs";
import { Graph } from "./graph.mjs";
import { Waterfall } from "./waterfall.mjs";

/** @typedef {import("../trace-reducer.mjs").RunView} RunView */
/** @typedef {import("../trace-reducer.mjs").GraphLayout} GraphLayout */

/**
 * @param {{
 *   run: RunView,
 *   layout: import("../trace-reducer.mjs").Loadable<GraphLayout>,
 *   onReplay: () => void,
 *   onSelectNode?: (name: string) => void,
 *   now?: number,
 * }} props `now`: the page's ticking clock, for a streaming run's headline (#156)
 */
export function Stage({ run, layout, onReplay, onSelectNode, now }) {
  const headline = runHeadline(run, now);
  return html`
    <div class="run" data-run-state=${run.status} data-dimmed=${String(headline.dimmed)}>
      <p class="visually-hidden" role="status" data-run-announcement>${runAnnouncement(run)}</p>
      <div class="run__head">
        ${headline.line
          ? html`<p class="run__line" data-run-line>${headline.line}</p>`
          : html`<p class="run__line run__line--idle">
              Pick a scenario and its path through the triage graph lights up here, node by node.
            </p>`}
        ${headline.scripted &&
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
