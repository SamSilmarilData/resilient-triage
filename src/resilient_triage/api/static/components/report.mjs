// @ts-check
// A run's answer (#119): the IncidentTriageReport as reportView() shapes it, or a shed
// request's notice. `data-report` says which; actions reconcile held for approval carry
// `data-raised="true"`.
import { html } from "htm/preact";

/** @typedef {import("../trace-reducer.mjs").ReportView} ReportView */

/** @param {{ view: ReportView }} props */
export function Report({ view }) {
  if (view.kind === "shed") {
    return html`<div class="report report--shed" data-report="shed">
      <p>${view.line}</p>
    </div>`;
  }
  return html`
    <article class="report" data-report="report" aria-label="Triage report">
      <div class="report__head">
        <span class="chip" data-severity>${view.severity}</span>
        <span class="chip" data-degradation data-tone=${view.tone}>${view.degradation}</span>
        <span class="report__confidence">Confidence <b>${view.confidence}</b></span>
      </div>
      <p class="report__summary">${view.summary}</p>
      <p class="report__provenance">${view.provenance}</p>
      <dl class="report__facts">
        <dt>Root cause</dt>
        <dd>${view.rootCause}</dd>
        <dt>Breakers tripped</dt>
        <dd>${view.breakers}</dd>
      </dl>
      ${view.actions.length > 0 &&
      html`<ol class="report__actions" aria-label="Recommended actions">
        ${view.actions.map(
          (action) => html`<li class="report__action" data-raised=${String(action.raised)}>
            <span class="report__priority">${action.priority}</span>
            <span class="report__what">
              ${action.action}
              <span class="report__target">${action.target} · ${action.type}</span>
            </span>
            <span class="report__approval">
              ${action.approval ? "Needs approval" : "No approval needed"}
              ${action.raised && html`<span class="report__raised">set by reconcile</span>`}
            </span>
          </li>`,
        )}
      </ol>`}
    </article>
  `;
}
