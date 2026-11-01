"""Fixtures for the browser tier (#117): a live app on loopback and guarded Chromium pages.

Test modules here set `pytestmark = pytest.mark.browser`, which pytest deselects by default;
`./scripts/check.sh --browser` runs them. This file is still collected in the regular run, so
Playwright is imported only inside fixtures: the regular CI job doesn't install it. When the
tier runs, a missing Playwright package or Chromium build errors every test: the run fails,
never skips.

The app runs in uvicorn on a background thread, on `127.0.0.1` and a free port, over the test's
own `AppRuntime`: a `ScriptedAdapter` model, a canned Statuspage and the deterministic embedder,
so free play never reaches Groq and nothing downloads a model. Chromium is a separate process
that `offline_only` can't see, so every context routes `**/*` through a guard that aborts any
request off the live app's own origin (scheme, host and port) and fails the test.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from harness import GuardedPages, HarnessError, LiveServer, serve_live_app

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Playwright


@pytest.fixture
def live_server() -> Iterator[LiveServer]:
    """The app on `127.0.0.1:<free port>` over a fresh scripted runtime, its cache emptied."""
    try:
        with serve_live_app() as live:
            yield live
    except HarnessError as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture(scope="session")
def playwright() -> Iterator[Playwright]:
    """Start Playwright; fails (never skips) when the `browser` extra isn't installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        pytest.fail(
            f"Playwright is not installed ({exc}): run pip install -e '.[test,browser]' "
            "and playwright install chromium",
            pytrace=False,
        )
    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def browser(playwright: Playwright) -> Iterator[Browser]:
    """Launch headless Chromium; fails (never skips) when it isn't installed."""
    from playwright.sync_api import Error

    try:
        chromium = playwright.chromium.launch()
    except Error as exc:
        pytest.fail(f"Chromium could not start: run playwright install chromium\n{exc}")
    yield chromium
    chromium.close()


@pytest.fixture
def pages(browser: Browser, live_server: LiveServer) -> Iterator[GuardedPages]:
    """Open guarded pages on the live app; fails the test if any page left the app's origin."""
    guarded = GuardedPages(browser, live_server)
    yield guarded
    guarded.close()
    assert not guarded.blocked(), f"the page tried to leave the app's origin: {guarded.blocked()}"
