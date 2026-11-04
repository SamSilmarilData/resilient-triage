// @ts-check
// The run's waterfall (#118): every step of the run as a bar on one shared clock. Dumb by
// design: rows, positions and words all come from trace-reducer.mjs `waterfallView()`. Setup
// draws left of the zero line, the run from it to the right.
import { html } from "htm/preact";
import { waterfallView } from "../trace-reducer.mjs";

/** @typedef {import("../trace-reducer.mjs").TraceView} TraceView */
/** @typedef {import("../trace-reducer.mjs").WaterfallRow} WaterfallRow */

/** @param {{ row: WaterfallRow }} props */
function Row({ row }) {
  return html`
    <li class="waterfall__row" data-kind=${row.kind} data-tone=${row.tone} data-depth=${row.depth}>
      <span class="waterfall__label">
        <span class="waterfall__name">${row.name}</span>
        <span class="waterfall__detail">
          ${row.detail}${row.tag && html` <span class="waterfall__tag">${row.tag}</span>`}
        </span>
      </span>
      <span class="waterfall__track">
        <span class="waterfall__bar" style=${`left:${row.leftPct}%;width:${row.widthPct}%`}></span>
      </span>
    </li>
  `;
}

/** @param {{ trace: TraceView }} props */
export function Waterfall({ trace }) {
  if (!trace.events.length) {
    return html`
      <div class="empty">
        <p class="empty__title">Timeline</p>
        <p class="empty__body">Each node, model call and telemetry read gets a bar on one shared clock.</p>
      </div>
    `;
  }
  const view = waterfallView(trace);
  return html`
    <figure class="waterfall" style=${`--zero:${view.zeroPct}%`}>
      <figcaption class="waterfall__caption">Timeline</figcaption>
      <ol class="waterfall__rows">
        ${view.rows.map((row) => html`<${Row} key=${row.key} row=${row} />`)}
      </ol>
      <div class="waterfall__axis" aria-hidden="true">
        <span></span>
        <span class="waterfall__track waterfall__track--axis">
          ${view.setupMs > 0 && html`<span class="waterfall__tick waterfall__tick--setup">setup</span>`}
          <span class="waterfall__zero"><span class="waterfall__tick">0</span></span>
          <span class="waterfall__tick waterfall__tick--end">${view.endLabel}</span>
        </span>
      </div>
    </figure>
  `;
}
