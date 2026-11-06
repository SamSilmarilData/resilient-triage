// @ts-check
// The live-trace stage (#118): one run's headline, its graph above its waterfall. Dumb by
// design: it draws what trace-reducer.mjs derives and calls back for Replay and Retry.
// `data-run-state` on the stage is the run's status; browser tests wait on it. Its one live
// region says only how a run ended (runAnnouncement), not the headline's running tick.
import { html } from "htm/preact";
import {
  graphView,
  runAnnouncement,
  runHeadline,
  runTicks,
  scenarioToggleId,
} from "../trace-reducer.mjs";
import { Graph } from "./graph.mjs";
import { Waterfall } from "./waterfall.mjs";

/** @typedef {import("../trace-reducer.mjs").RunView} RunView */
/** @typedef {import("../trace-reducer.mjs").GraphLayout} GraphLayout */
/** @typedef {import("@preact/signals").ReadonlySignal<number>} Clock */

// --- Only the time redraws on a tick ---

/**
 * Text that shows a time: the one component that reads the page's ticking clock, so a tick
 * redraws this text and nothing around it (no graph, timeline or panel). Render it only for
 * text that counts; `text` derives the words from the clock's wall-clock ms (a reducer view).
 * @param {{ now: Clock, text: (now: number) => string | null }} props
 */
export function Clocked({ now, text }) {
  return text(now.value);
}

/**
 * @param {{
 *   run: RunView,
 *   layout: import("../trace-reducer.mjs").Loadable<GraphLayout>,
 *   onReplay: () => void,
 *   onSelectNode?: (name: string) => void,
 *   now?: Clock,
 *   scenario?: import("../trace-reducer.mjs").StageScenario | null,
 *   onBack?: (id: string) => void,
 *   headOnly?: boolean,
 *   normalBackend?: string | null,
 *   announcement?: any,
 * }} props `now`: the page's ticking clock (a signal), read only by a streaming run's
 *   headline (#156). `announcement`: what the live region says instead of how the run ended
 *   (runAnnouncement()), e.g. free play's Clocked freePlayAnnouncement() during a countdown;
 *   the stage keeps one live region either way.
 *   `scenario`: the scenario the stage draws (stageScenario(); free play passes none), and
 *   `onBack` takes the visitor back to its card (#158). `headOnly`: draw the headline but
 *   no graph or timeline, for a run refused before any graph ran (#160). Free play only: the
 *   Shed request scenario keeps both, as its timeline shows the held slots and the wait its
 *   setup note describes, and its idle graph that the request never reached it.
 *   `normalBackend`: the cache backend normal for the run (normalCacheBackend()), for the
 *   timeline's cache rows
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
  announcement,
}) {
  const headline = runHeadline(run);
  return html`
    <div class="run" data-run-state=${run.status} data-dimmed=${String(headline.dimmed)}>
      <p class="visually-hidden" role="status" data-run-announcement>
        ${announcement || runAnnouncement(run)}
      </p>
      ${scenario && html`<${StageScenarioLine} scenario=${scenario} onBack=${onBack} />`}
      <div class="run__head">
        ${headline.line
          ? html`<p class="run__line" data-run-line>
              ${now && runTicks(run)
                ? html`<${Clocked} now=${now} text=${(/** @type {number} */ t) => runHeadline(run, t).line} />`
                : headline.line}
            </p>`
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

// --- The stage names its scenario ---

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
      href=${`#${scenarioToggleId(scenario.id)}`}
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
