// @ts-check
// One scenario in a chapter (#119): a disclosure whose heading button shows the title and
// caption. Opened, it shows the setup notes, Play and Skip to end, and once this scenario has
// played on the stage, the run's one-line summary, "Show everything" and the report. It never
// shows an expected path: only what the run's own trace says (#96).
import { html } from "htm/preact";
import { useState } from "preact/hooks";
import { scenarioPanelView } from "../trace-reducer.mjs";
import { Report } from "./report.mjs";

/** @typedef {import("../trace-reducer.mjs").ScenarioView} ScenarioView */
/** @typedef {import("../trace-reducer.mjs").RunView} RunView */

/**
 * @param {{
 *   scenario: ScenarioView,
 *   open: boolean,
 *   run: RunView | null,
 *   finishedOnce: boolean,
 *   onToggle: () => void,
 *   onPlay: () => void,
 *   onSkip: () => void,
 * }} props
 */
export function Scenario({ scenario, open, run, finishedOnce, onToggle, onPlay, onSkip }) {
  const panelId = `scenario-${scenario.id}-panel`;
  return html`
    <article class="scenario" data-scenario=${scenario.id} data-open=${String(open)}>
      <h4 class="scenario__heading">
        <button
          class="scenario__toggle"
          id=${`scenario-${scenario.id}-toggle`}
          type="button"
          aria-expanded=${String(open)}
          aria-controls=${panelId}
          onClick=${onToggle}
        >
          <span class="scenario__title">${scenario.title}</span>
          <span class="scenario__caption">${scenario.caption}</span>
        </button>
      </h4>
      <div class="scenario__panel" id=${panelId} hidden=${!open}>
        ${open &&
        html`<${ScenarioPanel}
          scenario=${scenario}
          run=${run}
          finishedOnce=${finishedOnce}
          onPlay=${onPlay}
          onSkip=${onSkip}
        />`}
      </div>
    </article>
  `;
}

/**
 * The opened scenario: setup notes, controls, then what this scenario's run showed.
 * Skip to end stays in place once there's nothing left to skip (aria-disabled, not
 * disabled), so a keyboard user's focus stays on it.
 * @param {{ scenario: ScenarioView, run: RunView | null, finishedOnce: boolean,
 *           onPlay: () => void, onSkip: () => void }} props
 */
function ScenarioPanel({ scenario, run, finishedOnce, onPlay, onSkip }) {
  const [everything, setEverything] = useState(false);
  const view = scenarioPanelView(run, finishedOnce);
  const eventsId = `scenario-${scenario.id}-events`;
  return html`
    ${scenario.setup_notes.length > 0 &&
    html`<ul class="scenario__setup" aria-label="Before the run">
      ${scenario.setup_notes.map((note) => html`<li>${note}</li>`)}
    </ul>`}
    <div class="scenario__controls">
      <button class="button button--primary" type="button" data-action="play" onClick=${onPlay}>
        ${view.playLabel}
      </button>
      <button
        class="button button--quiet"
        type="button"
        data-action="skip"
        aria-disabled=${String(view.skipDisabled)}
        onClick=${onSkip}
      >
        Skip to end
      </button>
    </div>
    ${view.line && html`<p class="scenario__line" data-scenario-line>${view.line}</p>`}
    ${view.report &&
    html`
      <button
        class="button button--quiet"
        type="button"
        data-action="show-everything"
        aria-expanded=${String(everything)}
        aria-controls=${eventsId}
        onClick=${() => setEverything(!everything)}
      >
        ${everything ? "Hide" : "Show"} everything (${view.events.length} events)
      </button>
      <ol
        class="events"
        id=${eventsId}
        hidden=${!everything}
        tabindex=${everything ? 0 : undefined}
        aria-label="Every trace event"
      >
        ${everything &&
        view.events.map(
          (row) => html`<li key=${row.seq} data-kind=${row.kind}>
            <span class="events__at">${row.at}</span>
            <span class="events__kind">${row.kind}</span>
            <span class="events__detail">${row.detail}</span>
          </li>`,
        )}
      </ol>
      <${Report} view=${view.report} />
    `}
  `;
}
