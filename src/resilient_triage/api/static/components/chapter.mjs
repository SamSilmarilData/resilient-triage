// @ts-check
// The guided chapters (#119): GET /scenarios drawn as chapters, each with its scenarios in
// catalogue order. All copy comes from the response (#101); none lives here. The region says
// how its catalogue load went in `data-catalogue` (loading, failed, ready).
import { html } from "htm/preact";
import { RetryNotice } from "./notice.mjs";
import { Scenario } from "./scenario.mjs";

/** @typedef {import("../trace-reducer.mjs").Catalogue} Catalogue */
/** @typedef {import("../trace-reducer.mjs").ScenarioView} ScenarioView */
/** @typedef {import("../trace-reducer.mjs").RunView} RunView */

/**
 * @typedef {{
 *   openId: string | null,
 *   runOf: (id: string) => RunView | null,
 *   onToggle: (id: string) => void,
 *   onPlay: (id: string) => void,
 *   onSkip: (id: string) => void,
 * }} ScenarioHandlers
 */

/**
 * @param {{ catalogue: import("../trace-reducer.mjs").Loadable<Catalogue>,
 *           onRetry: () => void } & ScenarioHandlers} props
 */
export function Chapters({ catalogue, onRetry, ...handlers }) {
  if (catalogue === "loading") {
    return html`<div class="chapters__state" data-catalogue="loading">
      <p class="chapters__loading">Loading the scenarios.</p>
    </div>`;
  }
  if (catalogue === "failed") {
    return html`<div class="chapters__state" data-catalogue="failed">
      <${RetryNotice}
        message="The scenarios didn't load, so the chapters can't open yet."
        onRetry=${onRetry}
      />
    </div>`;
  }
  return html`<div class="chapters__state" data-catalogue="ready">
    ${catalogue.chapters.map(
      (chapter, index) => html`<${Chapter} key=${chapter.id} chapter=${chapter} number=${index + 1} ...${handlers} />`,
    )}
  </div>`;
}

/**
 * One chapter: its number, title and intro, then its scenarios.
 * @param {{ chapter: Catalogue["chapters"][number], number: number } & ScenarioHandlers} props
 */
function Chapter({ chapter, number, openId, runOf, onToggle, onPlay, onSkip }) {
  const titleId = `chapter-${chapter.id}-title`;
  return html`
    <section class="chapter" data-chapter=${chapter.id} aria-labelledby=${titleId}>
      <h3 class="chapter__heading" id=${titleId}>
        <span class="chapter__number" aria-hidden="true">${number}</span>
        <span class="chapter__title">${chapter.title}</span>
      </h3>
      <p class="chapter__intro">${chapter.intro}</p>
      <ul class="chapter__scenarios">
        ${chapter.scenarios.map(
          (scenario) => html`<li key=${scenario.id}>
            <${Scenario}
              scenario=${scenario}
              open=${openId === scenario.id}
              run=${runOf(scenario.id)}
              onToggle=${() => onToggle(scenario.id)}
              onPlay=${() => onPlay(scenario.id)}
              onSkip=${() => onSkip(scenario.id)}
            />
          </li>`,
        )}
      </ul>
    </section>
  `;
}
