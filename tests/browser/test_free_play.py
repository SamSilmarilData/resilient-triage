"""Free play in Chromium (#120): your own question, its live trace in the free-play panel.

Every run here goes to the scripted harness runtime (a `ScriptedAdapter` model, a canned
Statuspage), and the route guard aborts anything off the app's origin, so no test reaches Groq.
Tests drive the form the way a visitor does and wait on the panel's `data-run-state`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from harness import (
    PAGE_WIDTHS,
    GuardedPages,
    axe_violations,
    describe,
    open_shell,
    run_selector,
    unique_question,
    wait_for_run,
)

from resilient_triage.config import Settings, settings
from resilient_triage.llm.scripted import DEFAULT_REPORT_JSON, ScriptedReply

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

pytestmark = pytest.mark.browser

PANEL = "#free-play-root"


def _panel(page: Page) -> Locator:
    """The free-play panel."""
    return page.locator(PANEL)


def _ask(page: Page, query: str) -> None:
    """Type a question into the free-play form and submit it."""
    panel = _panel(page)
    panel.get_by_label("Your incident").fill(query)
    panel.get_by_role("button", name="Triage").click()


def test_a_free_play_run_draws_its_trace_and_names_the_real_model(pages: GuardedPages):
    """The form posts to /triage/stream; graph and waterfall fill in the panel, unscripted."""
    page, watch = pages.open()
    open_shell(page)

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)

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
    assert page.locator(run_selector("idle")).count() == 1, "stage untouched"


def _budget_of(runs: int) -> Settings:
    """Settings allowing `runs` free-play graph runs a minute."""
    return settings.model_copy(update={"free_play_runs_per_minute": runs})


@pytest.mark.parametrize("live_settings", [_budget_of(1)], indirect=True)
def test_a_budget_shed_shows_its_reason_and_retry_after_then_recovers(pages: GuardedPages):
    """Over the budget the run is shed with its retry-after; the same page answers the next."""
    page, _ = pages.open()
    open_shell(page)
    page.evaluate("() => { window.notReloaded = true; }")
    panel = _panel(page)
    question = unique_question()

    _ask(page, question)
    wait_for_run(page, root=PANEL)
    _ask(page, unique_question("Checkout latency doubled after the 14:00 deploy"))
    page.wait_for_selector(f"{PANEL} [data-shed-reason]", state="attached")
    wait_for_run(page, root=PANEL)

    notice = panel.locator("[data-shed-reason]")
    assert notice.get_attribute("data-shed-reason") == "budget"
    retry_after = int(notice.get_attribute("data-retry-after") or "0")
    assert 1 <= retry_after <= 60
    assert notice.inner_text() == (
        f"The free-play budget is spent for this minute. Try again in {retry_after} s."
    )
    assert panel.locator("[data-run-line]").inner_text().startswith("Shed after ")
    assert panel.locator("[data-model]").count() == 0
    assert len(pages.server.llm.calls) == 1, "a shed run never reaches the model"

    # A cache hit doesn't count against the budget: the same page, unreloaded, answers it.
    _ask(page, question)
    page.wait_for_selector(f"{PANEL} [data-shed-reason]", state="detached")
    wait_for_run(page, root=PANEL)
    assert panel.locator("[data-run-line]").inner_text().startswith("Answered from cache")
    assert page.evaluate("() => window.notReloaded") is True


# page.evaluate script: stream a free-play run outside the panel, so it holds a run slot while
# its (delayed) model reply comes; window.slotHeld turns true once its run_slot is acquired.
HOLD_A_SLOT = """async (query) => {
  const sse = await import("/static/sse.mjs");
  window.slotHeld = false;
  window.holder = (async () => {
    for await (const m of sse.freePlaySource({query})(new AbortController().signal)) {
      if (m.kind === "event" && m.event.kind === "run_slot" && m.event.acquired) {
        window.slotHeld = true;
      }
    }
  })();
}"""


@pytest.mark.real_scripted_delays
@pytest.mark.parametrize(
    "live_settings",
    [settings.model_copy(update={"triage_max_concurrent_runs": 1, "triage_queue_timeout_s": 0.2})],
    indirect=True,
)
def test_a_capacity_shed_says_every_slot_is_busy_with_no_retry_after(pages: GuardedPages):
    """With every run slot held the run is shed for capacity: no retry-after, "in a moment"."""
    llm = pages.server.llm
    llm.script = [*llm.script[: len(llm.calls)], ScriptedReply(DEFAULT_REPORT_JSON, delay_s=3)]
    page, _ = pages.open()
    open_shell(page)
    page.evaluate(HOLD_A_SLOT, unique_question("Search indexing stalled since the reindex"))
    page.wait_for_function("() => window.slotHeld")

    _ask(page, unique_question())
    page.wait_for_selector(f"{PANEL} [data-shed-reason]", state="attached")
    wait_for_run(page, root=PANEL)

    notice = _panel(page).locator("[data-shed-reason]")
    assert notice.get_attribute("data-shed-reason") == "capacity"
    assert notice.get_attribute("data-retry-after") == ""
    assert notice.inner_text() == "Every run slot is busy right now. Try again in a moment."
    assert _panel(page).locator("[data-run-line]").inner_text().startswith("Shed after ")
    page.evaluate("() => window.holder")  # let the held run finish before the server stops


def test_a_network_error_is_an_inline_error_in_this_panel_with_retry(pages: GuardedPages):
    """A POST that never connects errors in the free-play panel only; Retry asks again."""
    page, watch = pages.open()
    open_shell(page)
    url = "**/triage/stream"
    page.route(url, lambda route: route.abort("connectionfailed"))

    _ask(page, unique_question())
    wait_for_run(page, "error", root=PANEL)

    panel = _panel(page)
    line = panel.locator("[data-run-line]").inner_text()
    assert line.startswith("Couldn't reach the server")
    assert panel.locator('[role="status"]').inner_text() == f"Run failed: {line}"
    assert page.locator(run_selector("idle")).count() == 1, "stage untouched"
    assert page.locator("#chapters-root .notice").count() == 0
    assert len(watch.failed) == 1

    page.unroute(url)
    panel.get_by_role("button", name="Retry").click()
    wait_for_run(page, root=PANEL)
    assert panel.locator("[data-run-line]").inner_text().startswith("Report in ")


def test_a_dropped_stream_is_lost_in_this_panel_then_replays(pages: GuardedPages):
    """A stream that closes before trace_finished keeps its partial trace, dimmed, with Replay."""
    page, _ = pages.open()
    open_shell(page)
    url = "**/triage/stream"
    page.route(
        url,
        lambda route: route.fulfill(
            status=200,
            content_type="text/event-stream",
            body='event: trace_started\ndata: {"seq": 0, "t_ms": 0, "kind": "trace_started", '
            '"schema_version": 1, "scenario_id": null, "scoped": false, '
            '"force_refresh": false}\n\n',
        ),
    )

    _ask(page, unique_question())
    wait_for_run(page, "lost", root=PANEL)

    panel = _panel(page)
    assert panel.locator("[data-run-line]").inner_text() == "Connection lost"
    assert panel.locator('[role="status"]').inner_text() == "Run stopped: Connection lost"
    assert panel.locator('.run[data-dimmed="true"]').count() == 1

    page.unroute(url)
    panel.get_by_role("button", name="Replay").click()
    wait_for_run(page, root=PANEL)
    assert panel.locator("[data-model]").inner_text() == f"Model: {settings.groq_model}"


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_finished_free_play_run_has_no_serious_axe_violations(
    pages: GuardedPages, color_scheme: str
):
    """The form, the model line and the panel's run pass axe-core in both themes."""
    page, _ = pages.open(color_scheme=color_scheme)
    open_shell(page)

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)

    violations = axe_violations(page)
    assert violations == [], describe(violations)


def test_a_free_play_run_fits_phone_width(pages: GuardedPages):
    """At 375 px the form and the panel's graph fit and nothing scrolls sideways."""
    page, _ = pages.open(viewport={"width": 375, "height": 812})
    open_shell(page)

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)

    widths = page.evaluate(PAGE_WIDTHS)
    assert widths[0] <= widths[1], f"page is {widths[0]} px wide in a {widths[1]} px viewport"
    graph = _panel(page).locator(".graph").bounding_box()
    assert graph is not None and graph["width"] >= 340


def test_a_free_play_graph_node_opens_the_inspector(pages: GuardedPages):
    """Free play's graph opens the node inspector too, by keyboard; Escape returns focus."""
    page, _ = pages.open()
    open_shell(page)
    page.wait_for_selector('[data-graph-nodes="ready"]', state="attached")

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)

    node = _panel(page).get_by_role("button", name="validate_report, visited once")
    node.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector('dialog.inspector[open][data-inspected="validate_report"]')
    assert page.locator(".inspector__title").inner_text() == "validate_report"

    page.keyboard.press("Escape")
    page.wait_for_selector("dialog.inspector[open]", state="detached")
    focused = page.evaluate(
        "document.activeElement.closest('#free-play-root [data-node]')?.dataset.node"
    )
    assert focused == "validate_report"
