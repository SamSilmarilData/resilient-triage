# Vendoring Preact, htm, Signals and the Tailwind v4 browser build without Node

Research for [#89](https://github.com/SamSilmarilData/resilient-triage/issues/89) (map: #86).
Gathered 2026-10-30. Versions, sizes and licenses come from registry.npmjs.org metadata and the
jsDelivr file-listing API (`data.jsdelivr.com/v1/packages/npm/<pkg>@<ver>?structure=flat`). I
downloaded the files to a scratch directory to check imports and gzip sizes. Nothing was installed
into the repo. "gz" means `gzip -9` of the exact file, measured locally.

Repo facts this answer depends on: the UI is one `src/resilient_triage/api/templates/index.html`
served by `GET /` as an `HTMLResponse` (`api/app.py`). There is no `StaticFiles` mount and no CSP
header yet. Tests block non-loopback sockets (`tests/conftest.py::offline_only`), and the `client`
fixture uses httpx `ASGITransport`, so it never opens a real socket.

## 1. Versions, files, sizes, licenses

| Package | Latest (published) | File to vendor | Bytes | gz | License |
|---|---|---|---|---|---|
| `preact` | 11.1.0 (2026-10-27) | `dist/preact.mjs` | 12,088 | 5,012 | MIT |
| `preact/hooks` | 11.1.0 | `hooks/dist/hooks.mjs` | 3,636 | 1,584 | MIT |
| `htm` | 3.1.1 (2022-04-26) | `dist/htm.mjs` | 1,207 | 646 | Apache-2.0 |
| `htm/preact` | 3.1.1 | `preact/index.mjs` | 147 | ~140 | Apache-2.0 |
| `@preact/signals` | 2.11.4 (2026-10-27) | `dist/signals.mjs` | 4,203 | 1,795 | MIT |
| `@preact/signals-core` | 1.14.5 (2026-10-27) | `dist/signals-core.mjs` | 5,756 | 2,092 | MIT |
| `@tailwindcss/browser` | 4.3.3 (2026-07-16) | `dist/index.global.js` | 282,289 | 73,740 | MIT |

- **Runtime total:** the six JS modules come to **27,037 bytes raw, about 11.3 KB gz**. Tailwind
  alone is about 6.5 times that.
- **Preact 11 is very new.** 11.0.0 shipped 2026-09-30 and 11.1.0 three days before this research.
  The 10.x line is at 10.29.8 (2026-08-01), whose `dist/preact.mjs` is 11,693 B and
  `hooks/dist/hooks.mjs` is 3,647 B. Per the
  [v11 upgrade guide](https://preactjs.com/guide/v11/upgrade-guide/), Preact 11 ships **ESM only
  (`.mjs`)**: "The `.module.js`, CommonJS, and UMD bundles that Preact 10.x provided have been
  removed." It targets Chrome 71+, Safari 12.1+, Firefox 69+ and Edge 79+. It also drops
  `render()`'s `replaceNode` argument and `Component.base`, and defers `useEffect` cleanup until
  after paint. None of these changes affects a greenfield htm app.
- **Signals supports both lines.** `@preact/signals@2.11.4` declares peer
  `preact: ">= 10.25.0 || >=11.0.0-0"` and depends on `@preact/signals-core ^1.14.5` (registry
  metadata). Version 2.0.0 had pinned `preact: 10.x`.
- **No signals-core version skew.** `@preact/signals` imports `@preact/signals-core` as a bare
  specifier and does not bundle it, so it has to be vendored and mapped too.
- **Licenses to ship.** Each vendored package's LICENSE file has to sit next to its files: MIT
  ~1.1 KB each, htm Apache-2.0 11,341 B. Apache-2.0 §4(a) requires shipping a copy of the license.
- **Bundled htm+Preact file.** `htm/preact/standalone.mjs` (13,194 B) bundles htm and Preact into
  one file, but it does **not** expose the same Preact instance that `@preact/signals` and hooks
  would import. **Don't use it** together with signals.
- **htm caching caveat** ([README](https://github.com/developit/htm)): "The default build of `htm`
  caches template strings", so a static subtree can be the same object in several places. `htm/mini`
  (852 B) disables this.

## 2. How the pieces import each other (checked in the shipped files)

```
dist/preact.mjs            -> (no imports)
hooks/dist/hooks.mjs       -> import { options } from "preact"
htm/dist/htm.mjs           -> (no imports)
htm/preact/index.mjs       -> import { h, Component, render } from "preact"; import htm from "htm"
signals/dist/signals.mjs   -> import { createElement, Fragment, Component, options, isValidElement } from "preact"
                              import { useMemo, useRef, useEffect } from "preact/hooks"
                              import { effect, Signal, computed, signal, batch } from "@preact/signals-core"
signals-core.mjs           -> (no imports)
```

`@preact/signals` integrates by patching Preact's `options` hooks, imported from `"preact"`, and
by calling `useMemo`/`useRef`/`useEffect` from `"preact/hooks"`. Hooks in turn patch
`options` from `"preact"`. So `"preact"`, `"preact/hooks"` and `"@preact/signals"` must all resolve
to the **same module URL**. If they don't, signals and hooks register on a second Preact copy and
silently do nothing.

## 3. The import map that keeps a single Preact instance

The Preact docs' [no-build page](https://preactjs.com/guide/v10/no-build-workflows/) uses esm.sh
with `?external=preact`: "we tell `esm.sh` that it shouldn't provide a copy of `preact`, we can
handle that ourselves". It also warns that "Preact must be used only as a singleton". That query
parameter is **esm.sh-specific**. Vendored files don't need it, because every file above already
imports Preact by its bare name. A single import map then gives each bare name exactly one URL:

```html
<script type="importmap">
{
  "imports": {
    "preact":               "/static/vendor/preact-11.1.0/preact.mjs",
    "preact/hooks":         "/static/vendor/preact-11.1.0/hooks.mjs",
    "htm":                  "/static/vendor/htm-3.1.1/htm.mjs",
    "htm/preact":           "/static/vendor/htm-3.1.1/preact.mjs",
    "@preact/signals-core": "/static/vendor/signals-core-1.14.5/signals-core.mjs",
    "@preact/signals":      "/static/vendor/signals-2.11.4/signals.mjs"
  }
}
</script>
```

Notes:
- **Use exact entries.** `htm/preact/index.mjs` can be renamed on vendoring because it only uses
  bare imports. The docs use a `"preact/": ".../"` prefix entry, but that only works when the
  vendored directory mirrors the package layout (`hooks/dist/hooks.mjs`). Exact entries are simpler
  to check in a test.
- **How a second instance happens.** It appears only if some module imports Preact by a *URL*
  (relative path, CDN URL, or a different `?v=` query) instead of the bare name. Module identity is
  the resolved URL. A Python test can assert this never happens (see §6a).
- **Version in the path doubles as cache busting.** The directory name carries the version, so
  long-lived `Cache-Control` headers are safe.
- **MIME type.** `.mjs` must be served as JavaScript. Starlette `StaticFiles` uses Python
  `mimetypes`, and CPython 3.12's built-in table maps `.mjs` → `text/javascript` (verified in the
  repo's 3.12.15 venv), so no extra config is needed.

## 4. `@tailwindcss/browser@4`

- **Not supported for production:** "The Play CDN is designed for development purposes only, and is
  not intended for production."
  ([docs](https://tailwindcss.com/docs/installation/play-cdn)). The documented tag is
  `<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>`.
- **It can be vendored and served same-origin.** The package is one self-contained classic-script
  IIFE, `dist/index.global.js` (`exports["."]`), 282,289 B / 73,740 gz, MIT. It makes no
  `fetch`/XHR calls. The source contains the default theme, preflight and utilities as strings, and
  `@import` of anything other than `tailwindcss`, `tailwindcss/theme`, `tailwindcss/preflight` or
  `tailwindcss/utilities` throws `The browser build does not support @import for "…"`.
  `@plugin`/config throws "The browser build does not support plugins or config files." (from the
  4.3.3 source).
- **Runtime cost** (read from the 4.3.3 source):
  - **Startup:** on load it compiles every `<style type="text/tailwindcss">` block in the page and
    then generates CSS for every class it finds in `document.querySelectorAll("[class]")`. It writes
    the output into one `<style>` element it appends to `<head>`.
  - **Ongoing:** a `MutationObserver` on `document.documentElement` (`subtree`, `childList`, and
    `class` attributes) triggers an incremental rebuild on every DOM insertion or class change, so
    every Preact render that adds nodes pays a class scan.
  - **Flash of unstyled content:** the build is async (a promise chain), so there is a FOUC before
    the first build.
  - **Inline styles only:** theme CSS must be **inline** `<style type="text/tailwindcss">`. The
    build ignores `<link>` stylesheets.
- **`@theme`, OKLCH and dark mode all work**, because the browser build is the full v4 compiler.
  `@theme` is documented directly for the Play CDN, using `<style type="text/tailwindcss">@theme { --color-… }`.
  The default palette is OKLCH ([colors docs](https://tailwindcss.com/docs/colors), e.g.
  `--color-red-50: oklch(97.1% 0.013 17.38)`; the 4.3.3 bundle has 572 `oklch(` literals), and
  custom `@theme` colors may be OKLCH. Dark mode defaults to `prefers-color-scheme`. To toggle it
  manually, use `@custom-variant dark (&:where(.dark, .dark *));` or
  `@custom-variant dark (&:where([data-theme=dark], [data-theme=dark] *));`
  ([dark-mode docs](https://tailwindcss.com/docs/dark-mode)). Both forms are handled in the bundle.
- **CSP caveat (inference, not verified against a browser):** the build writes generated CSS into a
  `<style>` element's `textContent`. Any future `Content-Security-Policy` therefore needs
  `style-src` to allow inline styles (or a nonce strategy).
- **Production path with no Node:** the Tailwind **standalone CLI** is "a self-contained executable
  — no Node.js or npm required" ([blog](https://tailwindcss.com/blog/standalone-cli); the post
  dates from v3, so its `init`/config steps are outdated). v4.3.3 ships one per platform on the GitHub
  release: `tailwindcss-linux-x64` 111,749,248 B, `tailwindcss-macos-arm64` 79,826,018 B. Running
  `tailwindcss -i in.css -o app.css --minify` once and committing `app.css` removes the runtime
  cost entirely, at the price of a regenerate step whenever class names change.

## 5. Fallbacks: elkjs and motion/mini

**elkjs 0.12.1** (2026-10-28), license **`EPL-2.0 OR GPL-3.0-or-later`** (weak copyleft, unlike
everything else here; flag it to the owner).
- **No ESM build.** `package.json` has only `main: lib/main` (no `module`/`exports`/`type`), and
  `lib/elk.bundled.js` is a browserify UMD that sets `window.ELK` when no module system is present.
  [README](https://github.com/kieler/elkjs): "ready to be dropped into a browser's <script> tag"
  → `new ELK()`.
- **Load as a classic `<script>`, not via the import map.**
- **Sizes:**
  - `elk.bundled.js`: 1,646,241 B / 477,137 gz.
  - Worker variant: `elk-api.js` 9,782 B plus `elk-worker.min.js` 1,631,868 B, with
    `new ELK({ workerUrl })`. This keeps layout off the main thread.
  - Either way the main payload is about 1.6 MB, roughly 60 times the whole Preact stack.

**motion 14.8.0** (2026-10-29), MIT.
- **mini (3.2 kB) cannot be vendored as one file.** The [docs](https://motion.dev/docs/animate)
  advertise "mini (3.2kb) and hybrid (22kb)" via `import { animate } from "motion/mini"`, but the
  3.2 kB is a bundled size. In the package, `dist/es/mini.mjs` is a 40-byte
  `export * from 'framer-motion/dom/mini'`. That chains into `framer-motion` →
  `./animation/animators/waapi/…` → `motion-dom` (496 ESM files) and `motion-utils`. Vendoring it
  without a bundler means mirroring several packages' trees and mapping three more bare names.
- **Mini's limits:** it animates HTML/SVG styles with native browser APIs. Independent transforms
  (`x`, `y`, …) are hybrid-only, and `repeatDelay` is "Not available in `animate` mini".
- **Single-file options:**
  1. `dist/motion.js`, the full hybrid UMD exposing `globalThis.Motion`: 178,676 B / 59,374 gz.
     This is what the [quick start](https://motion.dev/docs/quick-start) documents for script tags.
  2. A one-time download of jsDelivr's `motion@14.8.0/+esm`. jsDelivr builds that file itself, so
     it is not a registry file and its provenance is weaker.
  3. Calling `element.animate()` (WAAPI) directly, which is what mini wraps.

## 6. Testing htm templates with no Node in repo/CI/Docker

| Option | New deps | CI impact | Executes JS? | Fits `offline_only`? |
|---|---|---|---|---|
| (a) Python-only checks | none | none | no | yes, fully |
| (b) Playwright for Python | `pytest-playwright` (+ `playwright`) dev extra | browser download step, ~1 extra step | yes, real Chromium | yes, with a route guard |
| (c) Deno / Bun binary | a 42–52 MB binary in CI | setup action + separate step | yes, no DOM | Deno yes (deny-by-default net); Bun DOM needs npm packages |
| (d) In-browser test page | none | none (unless driven by b) | yes, manually | n/a |

**(a) Python-only checks.** Zero cost, run in today's pytest:
- **Integrity.** A `vendor/MANIFEST.json` lists `{package, version, source URL, sha256, license}`.
  A test re-hashes each file and checks its LICENSE is present. Reference SHA-256 hashes, measured
  on the downloaded files:
  - preact@11.1.0 `dist/preact.mjs`: `25288b8d9d60f7885df6c487a813a495e98c9addbeaf7c0e8f59683c71d426b5`
  - preact@11.1.0 `hooks/dist/hooks.mjs`: `24231102dc3e509c9c657bca591524e348eb8a5272c9d40538faef9beeeb654c`
  - htm@3.1.1 `dist/htm.mjs`: `ab33dd3f38059b9be4d5f5350128eefb2356639c4e0bbe9d9e8b3ba75847e9e4`
  - @preact/signals@2.11.4 `dist/signals.mjs`: `ba8177597f45acaca4d05dcf3966b527d0a8264cfa2b0a87ace92dff324217f8`
  - @preact/signals-core@1.14.5 `dist/signals-core.mjs`: `0c80bc127d72ecb1496ef116ffddae75079829a3af01e7f417c8e5a6474c68b6`
  - @tailwindcss/browser@4.3.3 `dist/index.global.js`: `a60c785630a06196808cbe79e6f7bdb4abcc8f4421a47b56f29338fc84805e3b`
- **Import-map closure.** Parse the `<script type="importmap">` JSON from `index.html` and regex
  every `from"…"` / `import"…"` specifier out of the vendored and app modules. Assert:
  1. every bare specifier is a key in the map,
  2. every map target exists on disk, and
  3. no module imports Preact by URL.

  This mechanically enforces the single-instance rule from §3.
- **Served assets** via the existing `client` fixture. Every map target and the Tailwind script
  `GET`s 200 with `content-type: text/javascript` and the intended cache headers, and `/` still
  renders.
- **Limit:** none of this executes JS, so template typos and runtime errors are not caught.

**(b) Playwright for Python.**
- **Packages:** `pytest-playwright` 0.10.0 (Apache-2.0) pulls in `playwright` 1.64.1 (Apache-2.0,
  Python ≥3.10). The Linux x86-64 wheel is 48,471,931 B.
- **It brings Node with it.** Platform wheels **bundle a Node.js binary** as the driver: setup.py
  says the build "adds the Node.js binary of NODE_VERSION". So Node arrives via pip, never in the
  repo, but it is present on the CI runner.
- **Browser download:** browsers come separately from Microsoft's CDN via `playwright install`. For
  CI the [docs](https://playwright.dev/python/docs/browsers) suggest
  `playwright install --with-deps --only-shell` (headless shell only). Example sizes listed: ~281 MB
  for Chromium. Cache `~/.cache/ms-playwright` (or `PLAYWRIGHT_BROWSERS_PATH`) keyed on the
  Playwright version. This is the same pattern as the existing FastEmbed download step: fetch
  before pytest, then run offline.
- **`offline_only` doesn't conflict:**
  - Python talks to the driver over stdio pipes (`PipeTransport`, `create_subprocess_exec` with
    `stdin/stdout=PIPE` in `playwright/_impl/transport.py`), and the patched `socket.connect` only
    affects the pytest process.
  - But that also means `offline_only` **does not see the browser's traffic**. Add
    `context.route("**/*", …)` that aborts and records any non-`127.0.0.1` request, so a stray CDN
    URL fails the test the same way `offline_only` does.
  - The browser can't use the `client` fixture's `ASGITransport`. Either run uvicorn on
    `127.0.0.1:<ephemeral>` in a thread (loopback is allowed), or answer every request from the ASGI
    app with `route.fulfill`.
  - The plugin's `page` fixture is sync and unaffected by `asyncio_mode = "auto"` for non-async tests.
- **Docker:** a dev-only extra, so the image is unaffected.

**(c) Deno or Bun as a single-binary runner.**
- **Binaries:** Deno 2.9.8 (`deno-x86_64-unknown-linux-gnu.zip` 41,963,064 B), Bun 1.4.4
  (`bun-linux-x64.zip` 51,794,595 B).
- **Deno and the vendored files:**
  - `deno.json` `"imports"` can map bare names to the vendored files ("The remapped name can be any
    valid specifier", [modules docs](https://docs.deno.com/runtime/fundamentals/modules/)). That
    makes it a second import map to keep in sync with `index.html`; (a) could check the two agree.
  - Permissions are deny-by-default: "any permission not explicitly granted at the command line will be
    denied" ([testing docs](https://docs.deno.com/runtime/fundamentals/testing/)). Running without
    `--allow-net` gives an offline guarantee for free.
  - Deno has **no DOM**. Tests can assert on htm → vnode trees and signal logic, or render to
    strings with a vendored `preact-render-to-string@6.8.2` `dist/index.mjs` (9,788 B, MIT, peer
    `preact >=10 || >= 11.0.0-0`).
  - Docs examples import `jsr:@std/assert`, which needs a download. Built-in `node:assert` avoids it.
- **Bun:** the documented DOM setup is `bun add -d @happy-dom/global-registrator` plus a
  `bunfig.toml` preload ([docs](https://bun.sh/docs/test/dom)). That puts a `package.json`/lockfile
  in the repo, which breaks the no-Node-tooling rule in spirit.
- **Cost either way:** a second language toolchain in CI and in contributors' setup, for less
  coverage than (b), since there is no real layout or CSS.

**(d) In-browser test page.** A dev-only route (e.g. `/static/tests.html`) imports the app modules
through the same import map, runs plain asserts, and prints pass/fail.
- **Pros:** zero deps, and it exercises the real map, real browser and real Tailwind.
- **Cons:** nothing runs it in CI by itself.
- **Pairs with (b):** one Playwright test can load the page and assert on `window.__testResults`,
  so browser-side tests stay in JS without adding a JS test runner.

**Weighing:**
- **(a)** is near-free and catches the failures specific to vendoring (missing or second-instance
  mappings, tampered files, wrong MIME). Do it regardless.
- **(b)** is the only option that proves the page renders in a browser. It costs one dev extra, a
  cached browser download step, and a route guard. Ask before adding it (CLAUDE.md: no deps
  without asking).
- **(d)** makes (b) cheap to extend.
- **(c)** adds a toolchain for the least benefit.

## Sources

- npm registry JSON: `registry.npmjs.org/{preact,htm,@preact/signals,@preact/signals-core,@tailwindcss/browser,elkjs,motion,framer-motion,motion-dom,preact-render-to-string}`
- jsDelivr file listings: `data.jsdelivr.com/v1/packages/npm/<pkg>@<ver>?structure=flat`; files from `cdn.jsdelivr.net/npm/<pkg>@<ver>/<path>`
- https://preactjs.com/guide/v10/no-build-workflows/ · https://preactjs.com/guide/v11/upgrade-guide/
- https://github.com/developit/htm · https://github.com/preactjs/signals (packages/preact/README.md)
- https://tailwindcss.com/docs/installation/play-cdn · https://tailwindcss.com/docs/dark-mode · https://tailwindcss.com/docs/colors · https://tailwindcss.com/blog/standalone-cli · GitHub release tailwindlabs/tailwindcss v4.3.3
- https://github.com/kieler/elkjs · https://motion.dev/docs/animate · https://motion.dev/docs/quick-start
- https://playwright.dev/python/docs/intro · https://playwright.dev/python/docs/browsers · pypi.org/pypi/{playwright,pytest-playwright}/json · github.com/microsoft/playwright-python (`setup.py`, `playwright/_impl/transport.py`)
- https://docs.deno.com/runtime/fundamentals/testing/ · https://docs.deno.com/runtime/fundamentals/modules/ · https://bun.sh/docs/test/dom · GitHub releases denoland/deno v2.9.8, oven-sh/bun bun-v1.4.4
