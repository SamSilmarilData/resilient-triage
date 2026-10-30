// @ts-check
// PROTOTYPE (issue #96). Variant B, "Studio": a three-pane workbench (LangGraph Studio /
// Inngest). Chapter rail left, graph over waterfall in the centre, inspector right.
import { html } from "htm/preact";
import { useState } from "preact/hooks";
import {
  CHAPTERS, data, scenario, pick, Graph, Waterfall, SummaryLine, EventLog, EventDetail, Report,
  Transport, SetupNotes, ScriptedBadge, ThemeToggle, selectedSeq, trace, useScrollSelected,
} from "./shared.js";

export const name = "Studio";

export function VariantB() {
  const d = data.value;
  const [tab, setTab] = useState("report");
  const logRef = useScrollSelected(selectedSeq.value);
  if (!d) return null;
  const sc = scenario.value;
  const selectTab = (/** @type {string} */ t) => setTab(t);
  const onNode = (/** @type {string} */ n) => {
    const e = trace.value?.events.find((/** @type {any} */ x) => x.kind === "node_finished" && x.node === n);
    if (e) { selectedSeq.value = e.seq; setTab("event"); }
  };
  return html`
    <div class="b">
      <header class="b__top">
        <h1 class="b__brand">resilient-triage</h1>
        <p class="b__hook">A triage agent that degrades instead of failing. Pick a scenario and inspect the real trace.</p>
        <${ThemeToggle} />
      </header>

      <nav class="b__rail" aria-label="Scenarios">
        <label class="b__select">
          <span class="visually-hidden">Scenario</span>
          <select id="b-scenario" value=${sc.id} onChange=${(/** @type {any} */ e) => pick(e.currentTarget.value)}>
            ${CHAPTERS.map((ch) => html`<optgroup label=${ch}>
              ${d.scenarios.filter((/** @type {any} */ s) => s.chapter === ch).map((/** @type {any} */ s) => html`<option value=${s.id}>${s.title}</option>`)}
            </optgroup>`)}
          </select>
        </label>
        ${CHAPTERS.map((ch) => html`
          <div class="b__group">
            <h2 class="b__group-title">${ch}</h2>
            ${d.scenarios.filter((/** @type {any} */ s) => s.chapter === ch).map((/** @type {any} */ s) => html`
              <button type="button" class="b__scn ${s.id === sc.id ? "is-on" : ""}" aria-current=${s.id === sc.id ? "true" : "false"} onClick=${() => pick(s.id)}>
                ${s.title}
              </button>`)}
          </div>`)}
        <div class="b__group">
          <h2 class="b__group-title">Free play</h2>
          <button type="button" class="b__scn" disabled>Your own query</button>
        </div>
      </nav>

      <main class="b__center">
        <div class="b__runbar">
          <div>
            <h2 class="b__title">${sc.title} <${ScriptedBadge} /></h2>
            <p class="muted small">${sc.caption}</p>
          </div>
          <${Transport} />
        </div>
        <${SetupNotes} />
        <${SummaryLine} />
        <div class="b__panes">
          <${Graph} onNode=${onNode} />
          <section class="b__wf" aria-label="Waterfall">
            <h3 class="b__pane-title">Timeline</h3>
            <${Waterfall} onSelect=${() => setTab("event")} />
          </section>
        </div>
      </main>

      <aside class="b__inspector" aria-label="Inspector">
        <div class="tabs" role="tablist" aria-label="Inspector views">
          ${[["report", "Report"], ["event", "Event"], ["log", "All events"]].map(([id, label]) => html`
            <button type="button" role="tab" id="b-tab-${id}" aria-selected=${tab === id} aria-controls="b-panel" class="tab" onClick=${() => selectTab(id)}>${label}</button>`)}
        </div>
        <div id="b-panel" role="tabpanel" aria-labelledby="b-tab-${tab}" class="b__panel" ref=${logRef}>
          ${tab === "report" && (html`<${Report} />` )}
          ${tab === "event" && html`<${EventDetail} />`}
          ${tab === "log" && html`<${EventLog} />`}
        </div>
      </aside>
    </div>`;
}
