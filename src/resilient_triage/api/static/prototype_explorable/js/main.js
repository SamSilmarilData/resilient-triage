// @ts-check
// PROTOTYPE (issue #96): three structurally different layouts of the explorable page, switched
// by ?variant=A|B|C and the floating bar (← / → also cycle). Throwaway, never merged.
import { html, render } from "htm/preact";
import { signal } from "@preact/signals";
import { load, data } from "./shared.js";
import * as A from "./variant-a.js";
import * as B from "./variant-b.js";
import * as C from "./variant-c.js";

const VARIANTS = { A: [A.VariantA, A.name], B: [B.VariantB, B.name], C: [C.VariantC, C.name] };
const KEYS = /** @type {("A"|"B"|"C")[]} */ (Object.keys(VARIANTS));
const initial = new URLSearchParams(location.search).get("variant")?.toUpperCase();
const variant = signal(KEYS.includes(/** @type {any} */ (initial)) ? /** @type {"A"|"B"|"C"} */ (initial) : "A");

function go(/** @type {number} */ step) {
  const i = (KEYS.indexOf(variant.value) + step + KEYS.length) % KEYS.length;
  variant.value = KEYS[i];
  const url = new URL(location.href);
  url.searchParams.set("variant", variant.value);
  history.replaceState(null, "", url);
}

addEventListener("keydown", (e) => {
  const t = /** @type {HTMLElement} */ (e.target);
  if (t.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "ArrowLeft") go(-1);
  if (e.key === "ArrowRight") go(1);
});

function Switcher() {
  const [, label] = VARIANTS[variant.value];
  return html`
    <div class="proto-bar" role="group" aria-label="Prototype variant">
      <button type="button" onClick=${() => go(-1)} aria-label="Previous variant">←</button>
      <span>${variant.value} · ${label}</span>
      <button type="button" onClick=${() => go(1)} aria-label="Next variant">→</button>
    </div>`;
}

function App() {
  const [View] = VARIANTS[variant.value];
  if (!data.value) return html`<p class="loading">Loading recorded traces…</p>`;
  return html`<div class="variant variant--${variant.value}"><${View} /></div><${Switcher} />`;
}

render(html`<${App} />`, /** @type {HTMLElement} */ (document.getElementById("app")));
load().catch((err) => {
  /** @type {HTMLElement} */ (document.getElementById("app")).textContent = `Could not load traces.json: ${err}`;
});
