"""Free play in Chromium (#120): your own question, its live trace in the free-play panel.

Every run here goes to the scripted harness runtime (a `ScriptedAdapter` model, a canned
Statuspage), and the route guard aborts anything off the app's origin, so no test reaches Groq.
Tests drive the form the way a visitor does and wait on the panel's `data-run-state`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from harness import GuardedPages, open_shell

from resilient_triage.config import settings

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

pytestmark = pytest.mark.browser

PANEL = "#free-play-root"
QUERY = "GitHub Actions workflows failing with 503s"


def _panel(page: Page) -> Locator:
    """The free-play panel."""
    return page.locator(PANEL)


def _ask(page: Page, query: str) -> None:
    """Type a question into the free-play form and submit it."""
    panel = _panel(page)
    panel.get_by_label("Your incident").fill(query)
    panel.get_by_role("button", name="Triage").click()


def _wait_for_panel_run(page: Page, state: str = "finished") -> None:
    """Wait until the free-play panel's run reaches a state; never a fixed sleep."""
    page.wait_for_selector(f'{PANEL} [data-run-state="{state}"]', state="attached")


def test_a_free_play_run_draws_its_trace_and_names_the_real_model(pages: GuardedPages):
    """The form posts to /triage/stream; graph and waterfall fill in the panel, unscripted."""
    page, watch = pages.open()
    open_shell(page)

    _ask(page, QUERY)
    _wait_for_panel_run(page)

    panel = _panel(page)
    assert panel.locator('[data-node="generate_report"][data-state="visited"]').count() == 1
    assert panel.locator('.waterfall__row[data-kind="model"]').count() == 1
    assert panel.locator("[data-model]").inner_text() == f"Model: {settings.groq_model}"
    assert panel.locator(".waterfall__tag").count() == 0, "free play has no scripted label"
    assert panel.locator("[data-run-line]").inner_text().startswith("Report in ")
    assert [url for url in watch.requests if url.endswith("/triage/stream")] == [
        f"{pages.server.url}/triage/stream"
    ]
    assert len(pages.server.llm.calls) == 1, "the scripted harness model answered"
    assert page.locator('#stage-root [data-run-state="idle"]').count() == 1, "stage untouched"
