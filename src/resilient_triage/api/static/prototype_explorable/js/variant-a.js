// @ts-check
// PROTOTYPE (issue #96). Variant A, "Essay": an explorable explanation you read top to bottom.
// Prose column on the left with the chapters inline; graph and waterfall stick on the right.
import { html } from "htm/preact";
import { useState } from "preact/hooks";
import {
  CHAPTERS, data, scenario, pick, Graph, Waterfall, SummaryLine, EventLog, EventDetail, Report,
  Transport, SetupNotes, ScriptedBadge, ThemeToggle, done, selectedSeq,
} from "./shared.js";

export const name = "Essay";

export function VariantA() {
  const d = data.value;
  const [open, setOpen] = useState(false);
  if (!d) return null;
  const sc = scenario.value;
  return html`
    <div class="a">
      <header class="a__hook">
        <div class="a__hook-inner">
          <p class="eyebrow">resilient-triage <${ThemeToggle} /></p>
          <h1>Break its dependencies. It still answers.</h1>
          <p class="lede">This incident-triage agent never returns a 500 and never hangs. Pick a scenario
            to watch a real run: every node, branch, model call and breaker below comes from the code
            that serves this page.</p>
        </div>
      </header>

      <main class="a__body">
        <section class="a__prose" aria-labelledby="a-guided">
          <h2 id="a-guided">Guided scenarios</h2>
          <p class="muted">Thirteen runs in four chapters, each a little worse than the last. <${ScriptedBadge} /></p>
          ${CHAPTERS.map((ch, ci) => html`
            <section class="a__chapter" aria-labelledby="a-ch-${ci}">
              <h3 id="a-ch-${ci}"><span class="a__chnum">${ci + 1}</span>${ch}</h3>
              <ul class="a__list">
                ${d.scenarios.filter((/** @type {any} */ s) => s.chapter === ch).map((/** @type {any} */ s) => {
                  const on = s.id === sc.id;
                  return html`
                    <li class="a__item ${on ? "is-on" : ""}">
                      <button type="button" class="a__pick" aria-expanded=${on} onClick=${() => pick(s.id)}>
                        <span class="a__title">${s.title}</span>
                        <span class="a__caption">${s.caption}</span>
                      </button>
                      ${on && html`
                        <div class="a__run">
                          <${SetupNotes} />
                          <p class="a__query"><span class="muted small">Query</span> ${s.query}</p>
                          <${Transport} />
                          <${SummaryLine} />
                          ${done.value && html`
                            <button type="button" class="btn btn--quiet btn--sm" aria-expanded=${open} onClick=${() => setOpen(!open)}>
                              ${open ? "Hide" : "Show"} everything (${d.scenarios.find((/** @type {any} */ x) => x.id === s.id).events.length} events)
                            </button>
                            ${open && html`<div class="a__everything"><${EventLog} upTo=${false} />${selectedSeq.value != null && html`<${EventDetail} />`}</div>`}
                            <${Report} />`}
                        </div>`}
                    </li>`;
                })}
              </ul>
            </section>`)}
        </section>

        <aside class="a__stage" aria-label="Live trace">
          <div class="a__sticky">
            <p class="a__stage-title"><b>${sc.title}</b> <span class="muted">live trace</span></p>
            <${Graph} compact />
            <${Waterfall} dense />
          </div>
        </aside>
      </main>

      <section class="a__more" aria-labelledby="a-free">
        <div class="a__more-inner">
          <h2 id="a-free">Free play</h2>
          <p>Ask your own question. Free play calls the real model, Statuspage and cache, with no faults.</p>
          <form class="free" onSubmit=${(/** @type {Event} */ e) => e.preventDefault()}>
            <label for="a-q" class="visually-hidden">Incident query</label>
            <textarea id="a-q" rows="2" placeholder="e.g. Stripe webhooks timing out with 504s"></textarea>
            <button type="submit" class="btn btn--primary" disabled>Triage</button>
            <p class="muted small">Not wired in this prototype.</p>
          </form>
          <h2>How it's built</h2>
          <ul class="built">
            <li><b>LangGraph</b> state machine with no checkpointer (ADR 0001, 0007)</li>
            <li><b>Circuit breakers and retries</b> around each telemetry source (ADR 0003)</li>
            <li><b>One LLM gateway</b> owning failover and cooldowns (ADR 0006)</li>
            <li><b>Execution trace</b> streamed over server-sent events (ADR 0008)</li>
          </ul>
        </div>
      </section>
    </div>`;
}
