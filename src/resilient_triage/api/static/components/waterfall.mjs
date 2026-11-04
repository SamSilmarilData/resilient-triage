// @ts-check
// The run's waterfall (#118): every step of the run on one shared clock.
import { html } from "htm/preact";

/** @typedef {import("../trace-reducer.mjs").TraceView} TraceView */

/** @param {{ trace: TraceView }} props */
export function Waterfall({ trace }) {
  void trace;
  return html`
    <div class="empty">
      <p class="empty__title">Timeline</p>
      <p class="empty__body">Each node, model call and telemetry read gets a bar on one shared clock.</p>
    </div>
  `;
}
