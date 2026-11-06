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
          ${row.detailParts.map(
            (part, i) => html`${i > 0 && " · "}<span class="waterfall__part">${part}</span>`,
          )}${row.tag && html` <span class="waterfall__tag">${row.tag}</span>`}
        </span>
      </span>
      <span class="waterfall__track">
        <span class="waterfall__bar" style=${`left:${row.leftPct}%;width:${row.widthPct}%`}></span>
      </span>
    </li>
  `;
}

/**
 * @param {{ trace: TraceView, normalBackend?: string | null }} props `normalBackend`: the
 *   cache backend normal for the run (normalCacheBackend()); cache rows name any other
 */
export function Waterfall({ trace, normalBackend = null }) {
  const view = waterfallView(trace, undefined, normalBackend);
  if (view === null) {
    return html`
      <div class="empty">
        <p class="empty__title">Timeline</p>
        <p class="empty__body">Each node, model call and telemetry read gets a bar on one shared clock.</p>
      </div>
    `;
  }
  return html`
    <figure class="waterfall" style=${`--zero:${view.zeroPct}%`}>
      <figcaption class="waterfall__caption">Timeline</figcaption>
      <ol class="waterfall__rows">
        ${view.rows.map((row) => html`<${Row} key=${row.key} row=${row} />`)}
      </ol>
      <div class="waterfall__axis" aria-hidden="true">
        <span></span>
        <span class="waterfall__track waterfall__track--axis">
          ${view.zeroPct > 0 && html`<span class="waterfall__tick waterfall__tick--setup">setup</span>`}
          <span class="waterfall__zero">
            ${view.showZeroLabel && html`<span class="waterfall__tick">0</span>`}
          </span>
          <span class="waterfall__tick waterfall__tick--end">${view.endLabel}</span>
        </span>
      </div>
    </figure>
  `;
}
