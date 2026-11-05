// @ts-check
// The free-play panel (#120): a visitor's own question, posted to POST /triage/stream, and its
// run drawn by its own Stage (the same graph and waterfall as the guided scenarios). Dumb by
// design: the request, the model line and the shed notice come from trace-reducer.mjs; the panel's run
// controller lives in app.mjs. Errors and lost streams stay in this panel (#101).
import { html } from "htm/preact";
import { answeringModel, freePlayRequest, shedNotice } from "../trace-reducer.mjs";
import { Stage } from "./stage.mjs";

/**
 * @template T
 * @typedef {import("../trace-reducer.mjs").Loadable<T>} Loadable
 */
/** @typedef {import("../trace-reducer.mjs").RunView} RunView */
/** @typedef {import("../trace-reducer.mjs").GraphLayout} GraphLayout */
/** @typedef {import("../sse.mjs").TriageRequest} TriageRequest */

/**
 * TriageRequest's limits (schemas/api.py), so the browser refuses a bad request first.
 * tests/browser/test_free_play.py fails if the drawn form drifts from its JSON schema.
 */
const REQUEST_LIMITS = Object.freeze({
  queryMinLength: 3,
  queryMaxLength: 2000,
  serviceMaxLength: 100,
  servicePattern: String.raw`[A-Za-z0-9_\s\-]+`,
});

/**
 * @param {{
 *   run: RunView,
 *   layout: Loadable<GraphLayout>,
 *   onSubmit: (request: TriageRequest) => void,
 *   onReplay: () => void,
 *   onSelectNode?: (name: string) => void,
 * }} props
 */
export function FreePlay({ run, layout, onSubmit, onReplay, onSelectNode }) {
  /** @param {SubmitEvent} event */
  const submit = (event) => {
    event.preventDefault();
    const form = /** @type {HTMLFormElement} */ (event.currentTarget);
    const data = new FormData(form);
    onSubmit(
      freePlayRequest({
        query: String(data.get("query") ?? ""),
        service_filter: String(data.get("service_filter") ?? ""),
        force_refresh: data.get("force_refresh") !== null,
      }),
    );
  };
  const model = answeringModel(run.trace);
  const shed = shedNotice(run.trace);
  return html`
    <div class="free-play__panel">
      <form class="ask" onSubmit=${submit}>
        <div class="ask__field">
          <label class="ask__label" for="free-play-query">Your incident</label>
          <textarea
            id="free-play-query"
            class="ask__input"
            name="query"
            rows="3"
            required
            minlength=${REQUEST_LIMITS.queryMinLength}
            maxlength=${REQUEST_LIMITS.queryMaxLength}
            placeholder="Stripe webhooks timing out with 504s since 14:05"
          ></textarea>
        </div>
        <div class="ask__row">
          <div class="ask__field">
            <label class="ask__label" for="free-play-service">Service (optional)</label>
            <input
              id="free-play-service"
              class="ask__input"
              name="service_filter"
              maxlength=${REQUEST_LIMITS.serviceMaxLength}
              pattern=${REQUEST_LIMITS.servicePattern}
              title="Letters, digits, spaces, hyphens and underscores"
              autocomplete="off"
            />
          </div>
          <label class="ask__check">
            <input type="checkbox" name="force_refresh" /> Skip the cache
          </label>
          <button class="button button--primary" type="submit">Triage</button>
        </div>
      </form>
      ${run.status !== "idle" &&
      html`
        ${shed &&
        html`<p
          class="notice"
          data-shed-reason=${shed.reason}
          data-retry-after=${shed.retryAfterS ?? ""}
        >${shed.line}</p>`}
        ${model && html`<p class="free-play__model" data-model>Model: ${model}</p>`}
        <div class="free-play__run">
          <${Stage}
            run=${run}
            layout=${layout}
            onReplay=${onReplay}
            onSelectNode=${onSelectNode}
          />
        </div>
      `}
    </div>
  `;
}
