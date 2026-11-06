// @ts-check
// The free-play panel (#120): a visitor's own question, posted to POST /triage/stream, and its
// run drawn by its own Stage (the same graph and waterfall as the guided scenarios). Dumb by
// design: the request, the model line, the shed notice and the report come from trace-reducer.mjs;
// the panel's run controller lives in app.mjs. A finished run's report sits under its stage, in
// the same card (#155). Errors and lost streams stay in this panel (#101).
import { html } from "htm/preact";
import { REQUEST_LIMITS, freePlayAnnouncement, freePlayView } from "../trace-reducer.mjs";
import { Report } from "./report.mjs";
import { Clocked, Stage } from "./stage.mjs";

/** @typedef {import("../trace-reducer.mjs").RunView} RunView */
/** @typedef {import("../trace-reducer.mjs").GraphLayout} GraphLayout */
/** @typedef {import("../trace-reducer.mjs").FreePlayFields} FreePlayFields */
/** @typedef {import("./stage.mjs").Clock} Clock */

/** The id of the form's message about a question too short to send (#160). */
const INPUT_ERROR_ID = "free-play-query-error";

/**
 * @param {{
 *   run: RunView,
 *   layout: import("../trace-reducer.mjs").Loadable<GraphLayout>,
 *   onSubmit: (fields: FreePlayFields) => void,
 *   onReplay: () => void,
 *   onSelectNode?: (name: string) => void,
 *   now?: Clock,
 *   inputError?: string | null,
 *   normalBackend?: string | null,
 * }} props `now`: the page's ticking clock (a signal), read only by what counts: the run's
 *   streaming headline (#156) and a budget shed's countdown, Triage button and the stage's
 *   live region saying Triage is back (#160). `onSubmit` gets the form's fields; the shell asks the
 *   reducer whether they make a request (freePlaySubmit) and passes back `inputError` when not.
 *   `normalBackend`: the cache backend normal for the run (normalCacheBackend())
 */
export function FreePlay({
  run,
  layout,
  onSubmit,
  onReplay,
  onSelectNode,
  now,
  inputError,
  normalBackend,
}) {
  /** @param {SubmitEvent} event */
  const submit = (event) => {
    event.preventDefault();
    const form = /** @type {HTMLFormElement} */ (event.currentTarget);
    const data = new FormData(form);
    onSubmit({
      query: String(data.get("query") ?? ""),
      service_filter: String(data.get("service_filter") ?? ""),
      force_refresh: data.get("force_refresh") !== null,
    });
  };
  const { model, shed, report, refused, retryAt } = freePlayView(run);
  // Only a budget shed's countdown changes with the clock; any other run reads none.
  const countdown = retryAt === null ? undefined : now;
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
            aria-invalid=${inputError ? "true" : undefined}
            aria-describedby=${inputError ? INPUT_ERROR_ID : undefined}
          ></textarea>
          ${inputError &&
          html`<p id=${INPUT_ERROR_ID} class="ask__error" role="alert" data-input-error>
            ${inputError}
          </p>`}
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
          <${TriageButton} run=${run} now=${countdown} />
        </div>
      </form>
      ${run.status !== "idle" &&
      html`
        ${shed &&
        html`<p
          class="notice"
          data-shed-reason=${shed.reason}
          data-retry-after=${shed.retryAfterS ?? ""}
        >${countdown
          ? html`<${Clocked}
              now=${countdown}
              text=${(/** @type {number} */ t) => freePlayView(run, t).shed?.line ?? null}
            />`
          : shed.line}</p>`}
        ${model && html`<p class="free-play__model" data-model>${model}</p>`}
        <div class="free-play__run">
          <${Stage}
            run=${run}
            now=${now}
            layout=${layout}
            onReplay=${onReplay}
            onSelectNode=${onSelectNode}
            headOnly=${refused}
            normalBackend=${normalBackend}
            announcement=${countdown &&
            html`<${Clocked}
              now=${countdown}
              text=${(/** @type {number} */ t) => freePlayAnnouncement(run, t)}
            />`}
          />
          ${report && html`<${Report} view=${report} />`}
        </div>
      `}
    </div>
  `;
}

// --- Only the time redraws on a tick ---

/**
 * Triage: unavailable while the panel's run streams or before a budget shed's retry time
 * (aria-disabled, so focus stays on it). Its own component, so a countdown's tick redraws
 * the button and not the panel.
 * @param {{ run: RunView, now?: Clock }} props `now`: given only while a countdown runs
 */
function TriageButton({ run, now }) {
  const { canTriage } = freePlayView(run, now?.value);
  return html`<button class="button button--primary" type="submit" aria-disabled=${String(!canTriage)}>
    Triage
  </button>`;
}
