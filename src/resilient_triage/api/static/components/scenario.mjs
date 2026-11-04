// @ts-check
// One scenario in a chapter (#119): a disclosure whose heading button shows the title and
// caption. Opened, it shows the setup notes, Play and Skip, and once this scenario has played
// on the stage, the run's one-line summary, "Show everything" and the report. It never shows
// an expected path: only what the run's own trace says (#96).
import { html } from "htm/preact";

/** @typedef {import("../trace-reducer.mjs").ScenarioView} ScenarioView */
/** @typedef {import("../trace-reducer.mjs").RunView} RunView */

/**
 * @param {{
 *   scenario: ScenarioView,
 *   open: boolean,
 *   run: RunView | null,
 *   next: ScenarioView | null,
 *   onToggle: () => void,
 *   onPlay: () => void,
 *   onSkip: () => void,
 * }} props
 */
export function Scenario({ scenario, open, onToggle }) {
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
        scenario.setup_notes.length > 0 &&
        html`<ul class="scenario__setup" aria-label="Before the run">
          ${scenario.setup_notes.map((note) => html`<li>${note}</li>`)}
        </ul>`}
      </div>
    </article>
  `;
}
