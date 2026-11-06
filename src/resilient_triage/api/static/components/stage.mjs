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
 *   scenario?: import("../trace-reducer.mjs").StageScenario | null,
 *   onBack?: (id: string) => void,
 *   headOnly?: boolean,
 *   normalBackend?: string | null,
 * }} props `now`: the page's ticking clock, for a streaming run's headline (#156).
 *   `scenario`: the scenario the stage draws (stageScenario(); free play passes none), and
 *   `onBack` takes the visitor back to its card (#158). `headOnly`: draw the headline but
 *   no graph or timeline, for a run refused before any graph ran (#160). `normalBackend`: the
 *   cache backend normal for the run (normalCacheBackend()), for the timeline's cache rows
 */
export function Stage({
  run,
  layout,
  onReplay,
  onSelectNode,
  now,
  scenario,
  onBack,
  headOnly,
  normalBackend,
}) {
  const headline = runHeadline(run, now);
  return html`
    <div class="run" data-run-state=${run.status} data-dimmed=${String(headline.dimmed)}>
      <p class="visually-hidden" role="status" data-run-announcement>${runAnnouncement(run)}</p>
      ${scenario && html`<${StageScenarioLine} scenario=${scenario} onBack=${onBack} />`}
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
      ${!headOnly &&
      html`<div class="run__panels">
        ${layout === "loading"
          ? null
          : layout === "failed"
            ? html`<p class="notice">The graph's layout didn't load. The timeline below still shows the run.</p>`
            : html`<${Graph} view=${graphView(layout, run.trace)} onSelectNode=${onSelectNode} />`}
        <${Waterfall} trace=${run.trace} normalBackend=${normalBackend} />
      </div>`}
    </div>
  `;
}

// --- #158: the stage names its scenario ---

/**
 * The scenario the stage draws, and once its run has finished, a link back to its card and
 * report (CSS shows it only below the wide breakpoint, where the card sits far above). The
 * link's href is the card's toggle, so it works as a plain fragment link too.
 * @param {{ scenario: import("../trace-reducer.mjs").StageScenario,
 *           onBack?: (id: string) => void }} props
 */
function StageScenarioLine({ scenario, onBack }) {
  return html`<div class="run__scenario">
    <p class="run__scenario-title" data-stage-scenario=${scenario.id}>${scenario.title}</p>
    ${scenario.offerBack &&
    html`<a
      class="run__back"
      href=${`#scenario-${scenario.id}-toggle`}
      data-stage-back
      onClick=${(/** @type {MouseEvent} */ event) => {
        if (!onBack) return;
        event.preventDefault();
        onBack(scenario.id);
      }}
      ><span aria-hidden="true">↑ </span>Back to the scenario's report</a
    >`}
  </div>`;
}
