"""Free play in Chromium (#120): your own question, its live trace in the free-play panel.

Every run here goes to the scripted harness runtime (a `ScriptedAdapter` model, a canned
Statuspage), and the route guard aborts anything off the app's origin, so no test reaches Groq.
Tests drive the form the way a visitor does and wait on the panel's `data-run-state`.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest
from harness import (
    PAGE_WIDTHS,
    GuardedPages,
    axe_violations,
    describe,
    open_shell,
    play,
    run_selector,
    unique_question,
    wait_for_run,
)

from resilient_triage.config import Settings, settings
from resilient_triage.llm.scripted import DEFAULT_REPORT_JSON, ScriptedReply
from resilient_triage.schemas.api import TriageRequest

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


# page.evaluate script: the free-play form's own limits, as the drawn fields carry them.
FORM_LIMITS = """() => {
  const query = document.querySelector("#free-play-query");
  const service = document.querySelector("#free-play-service");
  return {queryMin: query.minLength, queryMax: query.maxLength, serviceMax: service.maxLength};
}"""
# page.evaluate script: whether the service field's pattern accepts each value, as the browser
# checks it before a submit.
SERVICE_ACCEPTS = """(values) => {
  const service = document.querySelector("#free-play-service");
  const accepts = values.map((value) => {
    service.value = value;
    return !service.validity.patternMismatch;
  });
  service.value = "";
  return accepts;
}"""
# Service filters on both sides of TriageRequest's pattern.
SERVICE_PROBES = [
    "payments",
    "api-gateway",
    "auth_service 2",
    "EU West\t1",
    "-",
    "_",
    "a/b",
    "dot.name",
    "semi;colon",
    "café",
    "<script>",
    "emoji 🚀",
]


def test_the_form_refuses_what_triage_request_refuses(pages: GuardedPages):
    """The form's lengths and service pattern are TriageRequest's, so the browser refuses first."""
    properties = TriageRequest.model_json_schema()["properties"]
    query = properties["query"]
    (service,) = [option for option in properties["service_filter"]["anyOf"] if "pattern" in option]
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    limits = page.evaluate(FORM_LIMITS)
    accepts = page.evaluate(SERVICE_ACCEPTS, SERVICE_PROBES)

    assert limits == {
        "queryMin": query["minLength"],
        "queryMax": query["maxLength"],
        "serviceMax": service["maxLength"],
    }
    expected = [re.fullmatch(service["pattern"], value) is not None for value in SERVICE_PROBES]
    assert dict(zip(SERVICE_PROBES, accepts, strict=True)) == dict(
        zip(SERVICE_PROBES, expected, strict=True)
    )
    assert True in expected and False in expected, "the probes must sit on both sides"


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


# page.evaluate script (#155): the free-play run's card chrome, whether its graph and report
# sit inside the card's box, and the card's edges against the viewport.
RUN_CARD = """() => {
  const card = document.querySelector("#free-play-root .free-play__run");
  const style = getComputedStyle(card);
  const box = card.getBoundingClientRect();
  const within = (selector) => {
    const inner = card.querySelector(selector)?.getBoundingClientRect();
    return !!inner && inner.left >= box.left && inner.right <= box.right
      && inner.top >= box.top && inner.bottom <= box.bottom;
  };
  return {
    background: style.backgroundColor,
    borderTop: style.borderTopWidth,
    padding: style.paddingLeft,
    graphWithin: within(".graph"),
    reportWithin: within('[data-report="report"]'),
    left: box.left,
    right: box.right,
    viewport: document.documentElement.clientWidth,
  };
}"""
# The card's chrome on desktop, which phone width keeps.
CARD_CHROME = {"borderTop": "1px", "padding": "16px"}


@pytest.mark.parametrize(
    "viewport", [{"width": 1280, "height": 900}, {"width": 375, "height": 812}]
)
def test_a_free_play_run_keeps_its_card_at_every_width(
    pages: GuardedPages, viewport: dict[str, int]
):
    """The run, graph and report, sits in one surfaced, padded card on desktop and phone."""
    page, _ = pages.open(viewport=viewport)
    open_shell(page)

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)

    card = page.evaluate(RUN_CARD)
    assert card["background"] not in ("", "rgba(0, 0, 0, 0)"), card
    assert {key: card[key] for key in CARD_CHROME} == CARD_CHROME, card
    assert card["graphWithin"] and card["reportWithin"], card
    assert card["left"] >= 0 and card["right"] <= card["viewport"], card


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


# --- #153: the keyless simulation says so ---

KEYLESS = pytest.mark.parametrize("live_keyless", [True], indirect=True)
SIMULATED_MODEL_LINE = "Model: the keyless simulation (no model API key is set)"


@KEYLESS
def test_keyless_free_play_says_the_simulation_answered(pages: GuardedPages):
    """With no model API key, the model line and every model row name the simulation."""
    page, _ = pages.open()
    open_shell(page)

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)

    panel = _panel(page)
    assert panel.locator("[data-model]").inner_text() == SIMULATED_MODEL_LINE
    assert settings.groq_model not in panel.locator("[data-model]").inner_text()
    tags = panel.locator('.waterfall__row[data-kind="model"] .waterfall__tag').all_inner_texts()
    assert tags == ["simulated reply"]
    assert len(pages.server.llm.calls) == 1, "the keyless simulation answered"


@KEYLESS
def test_a_keyless_scenario_run_still_reads_scripted_reply(pages: GuardedPages):
    """Scenario runs keep their own scripted model, so keyless doesn't relabel them."""
    page, _ = pages.open()
    open_shell(page)

    play(page, scenario="healthy-run")
    wait_for_run(page)

    tags = page.locator(
        '#stage-root .waterfall__row[data-kind="model"] .waterfall__tag'
    ).all_inner_texts()
    assert tags and all(tag == "scripted reply" for tag in tags)


@KEYLESS
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_keyless_free_play_run_has_no_serious_axe_violations(
    pages: GuardedPages, color_scheme: str
):
    """The simulation's model line and tags pass axe-core in both themes."""
    page, _ = pages.open(color_scheme=color_scheme)
    open_shell(page)

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)
    assert _panel(page).locator("[data-model]").inner_text() == SIMULATED_MODEL_LINE

    violations = axe_violations(page)
    assert violations == [], describe(violations)


# --- #155: free play shows its report ---

MODEL_REPORT = json.loads(DEFAULT_REPORT_JSON)
REPORT = '[data-report="report"]'
CACHED = re.compile(r"Served from the semantic cache, similarity \d\.\d\d: no run, no model call\.")


def _report(page: Page) -> Locator:
    """The free-play panel's report, once its run has finished."""
    return _panel(page).locator(REPORT)


def test_a_finished_free_play_run_shows_its_report(pages: GuardedPages):
    """The answer reads like a scenario's report: severity to actions, from the model's reply."""
    page, _ = pages.open()
    open_shell(page)

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)

    report = _report(page)
    assert report.count() == 1
    assert report.locator("[data-severity]").inner_text() == MODEL_REPORT["severity"]
    assert report.locator("[data-degradation]").inner_text() == "HEALTHY"
    assert re.fullmatch(r"Confidence \d\.\d\d", report.locator(".report__confidence").inner_text())
    assert report.locator(".report__summary").inner_text() == MODEL_REPORT["summary"]
    assert report.locator(".report__provenance").inner_text() == (
        "Summary, analysis and actions are the model's reply. "
        "Status, breakers and approvals are set by reconcile."
    )
    facts = report.locator(".report__facts dd").all_inner_texts()
    assert facts[0] == MODEL_REPORT["root_cause_analysis"]
    actions = report.locator(".report__what").all_inner_texts()
    assert len(actions) == len(MODEL_REPORT["recommended_actions"])
    assert actions[0].startswith(MODEL_REPORT["recommended_actions"][0]["action"])


def test_a_free_play_cache_hit_shows_the_cached_report(pages: GuardedPages):
    """Asking the same question again is answered from the cache, and shows that report."""
    page, _ = pages.open()
    open_shell(page)
    question = unique_question()

    _ask(page, question)
    wait_for_run(page, root=PANEL)
    first = _report(page).locator(".report__summary").inner_text()

    _ask(page, question)
    page.wait_for_selector(f"{PANEL} .report__provenance >> text=Served from the semantic cache")
    wait_for_run(page, root=PANEL)

    report = _report(page)
    assert CACHED.fullmatch(report.locator(".report__provenance").inner_text())
    assert report.locator(".report__summary").inner_text() == first == MODEL_REPORT["summary"]
    assert len(pages.server.llm.calls) == 1, "the second answer made no model call"


@pytest.mark.parametrize("live_settings", [_budget_of(1)], indirect=True)
def test_a_shed_free_play_run_shows_its_notice_not_a_report(pages: GuardedPages):
    """A shed run has no report to show: its notice says why, once, with no report card."""
    page, _ = pages.open()
    open_shell(page)

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)
    assert _report(page).count() == 1
    _ask(page, unique_question())
    page.wait_for_selector(f"{PANEL} [data-shed-reason]")
    wait_for_run(page, root=PANEL)

    assert _panel(page).locator("[data-shed-reason]").count() == 1
    assert _panel(page).locator("[data-report]").count() == 0


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_free_play_report_has_no_serious_axe_violations(pages: GuardedPages, color_scheme: str):
    """The free-play report state passes axe-core in both themes."""
    page, _ = pages.open(color_scheme=color_scheme)
    open_shell(page)

    _ask(page, unique_question())
    wait_for_run(page, root=PANEL)
    assert _report(page).count() == 1

    violations = axe_violations(page)
    assert violations == [], describe(violations)
