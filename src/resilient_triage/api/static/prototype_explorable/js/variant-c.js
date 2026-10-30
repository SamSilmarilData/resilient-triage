// @ts-check
// PROTOTYPE (issue #96). Variant C, "Replay": one stage, one caption at a time. Chapters as
// tabs on top, the graph beside a large narrated caption, a scrubber, then a Temporal-style
// summary card and the report once the run ends. The waterfall sits behind a disclosure.
import { html } from "htm/preact";
import {
  CHAPTERS, data, scenario, pick, Graph, Waterfall, EventLog, Report, Transport, SetupNotes,
  ScriptedBadge, ThemeToggle, done, current, trace, describe, summarize, fmtMs, src, seen,
} from "./shared.js";

export const name = "Replay";

export function VariantC() {
  const d = data.value;
  if (!d) return null;
  const sc = scenario.value;
  const tr = trace.value;
  const chapter = sc.chapter;
  return html`
    <div class="c">
      <header class="c__hook">
        <p class="eyebrow">resilient-triage · an incident-triage agent <${ThemeToggle} /></p>
        <h1>What happens when everything it depends on breaks?</h1>
      </header>

      <nav class="c__chapters" aria-label="Chapters">
        <div class="c__tabs" role="tablist">
          ${CHAPTERS.map((ch, i) => html`
            <button type="button" role="tab" aria-selected=${ch === chapter} class="c__tab"
              onClick=${() => pick(d.scenarios.find((/** @type {any} */ s) => s.chapter === ch).id)}>
              <span class="c__tabnum">${i + 1}</span>${ch}
            </button>`)}
        </div>
        <div class="c__chips">
          ${d.scenarios.filter((/** @type {any} */ s) => s.chapter === chapter).map((/** @type {any} */ s) => html`
            <button type="button" class="chip-btn ${s.id === sc.id ? "is-on" : ""}" aria-pressed=${s.id === sc.id} onClick=${() => pick(s.id)}>${s.title}</button>`)}
        </div>
      </nav>

      <main class="c__stage">
        <div class="c__graph"><${Graph} /></div>
        <div class="c__narration">
          <p class="muted small">${sc.caption} <${ScriptedBadge} /></p>
          <${SetupNotes} />
          <p class="c__caption" aria-live="polite">${current.value ? describe(current.value, tr.events) : "Ready."}</p>
          <ol class="c__recent" aria-label="Earlier steps">
            ${seen.value.filter((/** @type {any} */ e) => e.kind !== "node_started").slice(-6, -1).reverse().map((/** @type {any} */ e) => html`<li>${describe(e, tr.events)}</li>`)}
          </ol>
          <${Transport} scrub />
        </div>
      </main>

      ${done.value && html`
        <section class="c__result" aria-label="Result">
          <${KeyFacts} />
          <${Report} />
        </section>`}

      <details class="c__more">
        <summary>Timing and every event</summary>
        <${Waterfall} />
        <${EventLog} upTo=${false} />
      </details>

      <footer class="c__foot">
        <p><b>Free play</b> lets you ask your own question against the real model (not wired in this prototype).</p>
      </footer>
    </div>`;
}

/** Temporal-style key/value summary of the finished trace. */
function KeyFacts() {
  const tr = trace.value;
  const ev = tr.events;
  const { head } = summarize(tr);
  const calls = ev.filter((/** @type {any} */ e) => e.kind === "model_call");
  const reads = ev.filter((/** @type {any} */ e) => e.kind === "telemetry_read" && e.phase === "run");
  const path = ev.filter((/** @type {any} */ e) => e.kind === "node_started").map((/** @type {any} */ e) => e.node);
  const rec = ev.find((/** @type {any} */ e) => e.kind === "reconciled");
  return html`
    <dl class="facts">
      <div><dt>Outcome</dt><dd>${head}</dd></div>
      <div><dt>Steps</dt><dd>${path.length ? `${path.length} nodes` : "no run"}</dd></div>
      <div><dt>Model calls</dt><dd>${calls.length ? calls.map((/** @type {any} */ c) => `${c.role} ${c.outcome === "completion" ? "✓" : "✗"}`).join(" · ") : "none"}</dd></div>
      <div><dt>Telemetry</dt><dd>${reads.length ? reads.map((/** @type {any} */ r) => `${src(r.source)} ${r.outcome}`).join(" · ") : "none"}</dd></div>
      <div><dt>Reconciled</dt><dd>${rec ? (rec.changes.length ? `${rec.changes.length} change${rec.changes.length > 1 ? "s" : ""}` : "no changes") : "n/a"}</dd></div>
      <div><dt>Total</dt><dd class="mono">${fmtMs(tr.end)}</dd></div>
    </dl>`;
}
