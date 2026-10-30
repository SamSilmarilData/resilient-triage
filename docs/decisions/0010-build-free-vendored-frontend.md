# ADR-010: Build-Free, Vendored Frontend

## Status
Accepted

## Date
2026-10-31

## Context
The UI rebuild (map issue #86) replaces the current page with an explorable explanation of a
triage run: a graph, a waterfall and guided scenarios driven by the execution trace (ADR-008).
It is served by the same FastAPI app as a public portfolio deployment, so it must stay small,
load fast, look clean and work at phone width with WCAG AA. The repo is Python only: there is
no Node, bundler or `package.json` in the repo, CI or Docker image, and the map rules one out.
The current page pulls Tailwind's Play CDN script from a third-party origin. The vendoring facts
this decision relies on are in "Vendoring Preact, htm, Signals and Tailwind v4 browser build
without Node" (issue #89). The dependencies were approved in "Frontend dependency approval"
(issue #94) and the testing strategy in "Testing the build-free frontend" (issue #95).

## Decision
The frontend is **build-free and vendored**: native ES modules, one hand-written stylesheet and
self-hosted fonts, all served same-origin by the app, with no build step.

- **Runtime packages** (about 11 KB gzipped), loaded through **one import map** with **one
  same-origin URL per bare name**, so the page has a single Preact instance:

  | Package | Version | License |
  |---|---|---|
  | `preact` (+ `preact/hooks`) | 10.29.8 | MIT |
  | `htm` (+ `htm/preact`) | 3.1.1 | Apache-2.0 (LICENSE shipped) |
  | `@preact/signals` | 2.11.4 | MIT |
  | `@preact/signals-core` | 1.14.5 | MIT |

  Never use `htm/preact/standalone`: it carries a second copy of Preact.
- **Fonts.** Inter and JetBrains Mono, both variable woff2 with a Latin subset (OFL-1.1),
  self-hosted with `font-display: swap` and pinned in the manifest. No Google Fonts.
- **Layout.** Page assets live in `src/resilient_triage/api/static/`, mounted with `StaticFiles`
  at `/static`, and package-data grows to include `static/**`. Third-party files go in
  `static/vendor/<package>@<version>/<file>` with the package's LICENSE alongside. Versioned paths
  allow `Cache-Control: immutable`.
- **Updates.** `static/vendor/manifest.json` records each file's name, version, source URL,
  sha256 and license file. A stdlib-only `scripts/vendor.py update` downloads, verifies and
  rewrites the manifest. Updates are manual and deliberate; there is no Dependabot for vendored
  files.
- **Zero third-party requests.** The page fetches nothing from another origin, and a test asserts
  the HTML and CSS reference no external origin. The Tailwind Play-CDN script goes away with the
  rebuild.
- **Styling.** One hand-written stylesheet: OKLCH custom-property tokens on `:root`, a dark
  theme, `@layer` and native CSS nesting.
- **Motion.** CSS transitions, WAAPI (`element.animate()`) and View Transitions, all gated on
  `prefers-reduced-motion`. Browsers without View Transitions simply don't animate.

### Testing
Two tiers. The Python tier always runs in `./scripts/check.sh`; the browser tier is opt-in
locally and required in CI.

**Python tier** (offline, no new dependencies):
- **Static assets** (`tests/test_static_assets.py`): every vendored file matches its
  `manifest.json` sha256 and has its LICENSE alongside. **Import-map closure**: a regex over
  `import … from "…"` and `import("…")` in our own and vendored `.mjs` checks that every bare name
  resolves through the import map to a file that exists, that nothing imports Preact by URL and
  that `htm/preact/standalone` is never used. The HTML and CSS reference no external origin.
  Served through the `client` fixture, assets have the correct MIME types and versioned
  `vendor/` paths carry `Cache-Control: immutable`.
- **Layout covers topology**: hand-placed positions live in a data file,
  `static/graph-layout.json` (node to x/y, plus edge routing hints), which the page fetches. A
  **two-way** check against `triage_graph.get_graph()` fails on a missing node or edge and on a
  stale one. Conditional edges are keyed by router label, matching ADR-008.
- **Scenario streams**: one parametrized test over the whole catalogue via
  `POST /scenarios/{id}/stream` on the `client` fixture parses the SSE frames and asserts that the
  stream ends in `trace_finished`, every event validates against the trace schema, the
  `TraceSummary` matches the catalogue's expected outcome, `scenario_id` is set and `/metrics` is
  unchanged. It sits alongside the graph-level catalogue test.

**Browser tier** (Playwright for Python, Chromium only, approved as a new dev extra):
- **Where it runs**: tests carry `@pytest.mark.browser` and pytest `addopts` deselects them by
  default (`-m "not browser"`). `./scripts/check.sh --browser` runs them locally, and a separate
  **required** CI job, `browser`, runs them with a cached `~/.cache/ms-playwright` and
  `playwright install --with-deps --only-shell chromium`. When they are requested and Chromium is
  missing, they fail, never skip.
- **Harness**: uvicorn in a background thread on `127.0.0.1:<free port>`, serving an app built
  from the test's own `AppRuntime` (`ScriptedAdapter` and canned Statuspage, so free play never
  reaches Groq); `create_app` accepts an injected runtime. Every browser context gets a
  `context.route("**/*")` **off-origin route guard** that aborts non-loopback requests and fails
  the test, the browser-side counterpart of `offline_only`.
- **Coverage** (deliberately thin, since the scenario-stream test covers every scenario
  server-side): the page loads with no console errors and no failed or off-origin requests; one
  scenario per chapter (four) plays end to end, reaching its terminal state with visited nodes
  and taken edges matching the summary and the "model replies are scripted" label shown; one
  free-play run against the scripted runtime; the guided flow (hook, scenario, free play) can be
  completed by keyboard alone with focus visible; no horizontal page scroll at 375 px in light,
  dark and `prefers-reduced-motion: reduce`.
- **Reducer**: the trace-to-view reducer is the only JS logic module. The test imports it with
  `page.evaluate` and folds in the events it just streamed from the live server. There are no
  committed golden fixtures.
- **Accessibility**: `axe-core` (MPL-2.0, a single `axe.min.js`) is approved as a **test-only**
  dependency, vendored under `tests/vendor/axe-core@<ver>/` and pinned in a test-side sha256
  manifest. It is injected with `page.add_script_tag`, never served by the app, and runs on the
  landing state and each covered scenario's final state, in light and dark. It fails on `serious`
  or `critical` violations, including contrast for the WCAG AA claim.

**No JS lint or type-check** runs in CI, because Node, Deno and Bun are all out. To compensate,
JS logic stays in the one reducer module, every one of our own (non-vendored) JS files starts with
`// @ts-check` and uses JSDoc types, and new JS logic needs a browser test (`CODING_STANDARDS.md`, "Frontend").
Screenshot and visual-regression testing is not decided here (issue #101).

## Considered Options
- **Tailwind v4 browser build (`@tailwindcss/browser`).** Rejected: officially dev-only, 282 KB,
  it compiles at runtime, causes a flash of unstyled content and rebuilds through a
  `MutationObserver` on every class change.
- **Tailwind standalone CLI.** Rejected: a 112 MB binary plus a check that the committed CSS has
  not drifted from the build, to replace one stylesheet we can write by hand.
- **elkjs for graph layout.** Rejected: the layout is hand-placed and checked against the real
  topology. elkjs is also 1.6 MB, UMD-only and EPL/GPL.
- **motion.** Rejected: the mini build is not vendorable as a single file and the UMD build is
  59 KB gzipped. CSS, WAAPI and View Transitions cover the motion we want.
- **Any Node toolchain or bundler** (Svelte, Solid, React with Vite, npm-managed Preact). Rejected:
  it adds a second language toolchain to the repo, CI and Docker image for a page that needs no
  compile step.
- **Preact 11 (11.1.0).** Rejected for now: it is one month old and docs and examples still
  target 10. Moving to 11 later is a manifest bump.

## Consequences
- No build step: editing a `.mjs` or the stylesheet and reloading is the whole loop, and the
  Docker image gains static files, not a toolchain.
- Every vendored byte is pinned, hashed and licensed in the repo. Upgrading is a deliberate
  `scripts/vendor.py update` plus a manifest diff, and nothing upgrades on its own.
- The page works offline from the app and leaks no visitor traffic to a CDN or font host.
- Without JS lint or type-check, JS correctness rests on keeping logic in the reducer, `@ts-check`
  in editors, and browser tests. CI gains a required `browser` job and Playwright as a dev extra.
- Styling is hand-written, so design tokens and the dark theme are maintained in one stylesheet
  rather than generated from utility classes.
- A new vendored runtime or test package needs explicit approval, like any other dependency.
