"""The guided chapters in Chromium (#119): chapters and scenarios drawn from GET /scenarios.

A scenario opens inline (its setup notes, Play and Skip to end, its one-line summary, "Show
everything" and the report) and plays on the pinned stage. Everything the page shows is
checked against the live app's own catalogue and the Python summary of the same run.
"""

import json
from typing import TYPE_CHECKING, Any

import pytest
from harness import (
    CATALOGUE_READY,
    GRAPH_READY,
    GuardedPages,
    axe_violations,
    describe,
    graph_state,
    open_shell,
    path_edges,
    stream_scenario,
    wait_for_run,
)

from resilient_triage.scenarios.catalogue import get_scenario, public_catalogue
from resilient_triage.trace.events import Reconciled, TraceFinished
from resilient_triage.trace.summary import summarize

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

# The chapters as drawn: each chapter's id, title and intro, and its scenarios' copy.
DRAWN_CHAPTERS = """() => [...document.querySelectorAll("[data-chapter]")].map((chapter) => ({
  id: chapter.dataset.chapter,
  title: chapter.querySelector(".chapter__title").textContent.trim(),
  intro: chapter.querySelector(".chapter__intro").textContent.trim(),
  scenarios: [...chapter.querySelectorAll("[data-scenario]")].map((scenario) => ({
    id: scenario.dataset.scenario,
    title: scenario.querySelector(".scenario__title").textContent.trim(),
    caption: scenario.querySelector(".scenario__caption").textContent.trim(),
  })),
}))"""
# page.evaluate script: record every state the stage draws from here on into
# window.stageFrames (run state, headline, waterfall rows, lit graph nodes).
RECORD_STAGE = """() => {
  const root = document.querySelector("#stage-root");
  const frame = () => ({
    state: root.querySelector(".run")?.dataset.runState ?? null,
    line: root.querySelector("[data-run-line]")?.textContent ?? null,
    rows: root.querySelectorAll(".waterfall__row").length,
    visited: root.querySelectorAll('[data-node]:not([data-state="idle"])').length,
  });
  window.stageFrames = [];
  new MutationObserver(() => window.stageFrames.push(frame()))
    .observe(root, {subtree: true, childList: true, attributes: true, characterData: true});
}"""
# One scenario per chapter, in chapter order.
PLAYED = ["healthy-run", "reconciliation", "shed-request", "everything-down"]


def _open_chapters(page: "Page") -> None:
    """Load the page and wait until the chapters have drawn from GET /scenarios."""
    open_shell(page)
    page.wait_for_selector(CATALOGUE_READY, state="attached")


def _copy(catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    """The copy the chapters should draw from a GET /scenarios body."""
    return [
        {
            "id": chapter["id"],
            "title": chapter["title"],
            "intro": chapter["intro"],
            "scenarios": [
                {"id": s["id"], "title": s["title"], "caption": s["caption"]}
                for s in chapter["scenarios"]
            ],
        }
        for chapter in catalogue["chapters"]
    ]


def test_chapters_draw_the_catalogue_from_get_scenarios(pages: GuardedPages):
    """Every chapter and scenario on the page is the catalogue's, in its order."""
    page, watch = pages.open()

    _open_chapters(page)

    expected = _copy(public_catalogue().model_dump(mode="json"))
    assert page.evaluate(DRAWN_CHAPTERS) == expected
    assert any(url.endswith("/scenarios") for url in watch.requests)
    assert watch.failed == []


def test_the_copy_comes_from_the_response_not_the_page(pages: GuardedPages):
    """Change the served catalogue and the page draws the change: no copy lives in JS."""
    body = public_catalogue().model_dump(mode="json")
    body["chapters"] = body["chapters"][:1]
    body["chapters"][0]["title"] = "Renamed chapter"
    body["chapters"][0]["scenarios"][0]["caption"] = "A caption only the response knows."
    page, _ = pages.open()
    page.route(
        "**/scenarios",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)
        ),
    )

    _open_chapters(page)

    assert page.evaluate(DRAWN_CHAPTERS) == _copy(body)


def test_a_failed_catalogue_keeps_the_prose_and_offers_retry(pages: GuardedPages):
    """GET /scenarios failing leaves the static prose up and a notice with Retry (#101)."""
    page, watch = pages.open()
    page.route("**/scenarios", lambda route: route.fulfill(status=503, body="down"))

    open_shell(page)
    page.wait_for_selector('[data-catalogue="failed"]', state="attached")

    assert page.locator("#hook-title").is_visible()
    assert page.locator("#guided-title").is_visible()
    notice = page.locator('[data-catalogue="failed"] [role="alert"]')
    assert "didn't load" in notice.inner_text()
    assert [failure.split(" ")[0] for failure in watch.failed] == ["503"]

    page.unroute("**/scenarios")
    page.get_by_role("button", name="Retry").click()
    page.wait_for_selector(CATALOGUE_READY, state="attached")
    assert page.locator("[data-chapter]").count() == len(public_catalogue().chapters)


def _scenario_selector(scenario_id: str, selector: str = "") -> str:
    """A selector inside one scenario's disclosure."""
    return f'[data-scenario="{scenario_id}"] {selector}'.strip()


def _scenario(page: "Page", scenario_id: str, selector: str = "") -> Any:
    """A locator inside one scenario's disclosure."""
    return page.locator(_scenario_selector(scenario_id, selector))


def test_opening_a_scenario_shows_its_setup_and_never_an_expected_path(pages: GuardedPages):
    """Opened, a scenario shows its setup notes and Play; the graph stays idle until it runs."""
    scenario = get_scenario("tripped-breaker")
    assert scenario is not None
    page, _ = pages.open()
    _open_chapters(page)
    page.wait_for_selector(GRAPH_READY, state="attached")

    toggle = page.locator("#scenario-tripped-breaker-toggle")
    toggle.click()

    assert toggle.get_attribute("aria-expanded") == "true"
    notes = _scenario(page, scenario.id, ".scenario__setup li").all_inner_texts()
    assert notes == list(scenario.setup_notes)
    assert _scenario(page, scenario.id, '[data-action="play"]').is_visible()
    assert page.locator('[data-scenario][data-open="true"]').count() == 1
    graph = graph_state(page)
    assert graph.taken == []
    assert set(graph.visits.values()) == {0}
    assert _scenario(page, scenario.id, "[data-report]").count() == 0

    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "false"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("scenario_id", PLAYED)
def test_a_played_scenario_ends_with_its_trace_summary_and_report(
    pages: GuardedPages, scenario_id: str, scheme: str
):
    """Play streams the run onto the stage; the scenario then shows its line and report."""
    events = stream_scenario(pages.server, scenario_id)
    summary = summarize(events)
    finished = events[-1]
    assert isinstance(finished, TraceFinished)
    path = [visit.node for visit in summary.nodes]
    page, watch = pages.open(color_scheme=scheme)
    _open_chapters(page)
    page.wait_for_selector(GRAPH_READY, state="attached")

    page.locator(f"#scenario-{scenario_id}-toggle").click()
    _scenario(page, scenario_id, '[data-action="play"]').click()
    wait_for_run(page)

    graph = graph_state(page)
    assert graph.visited == set(path)
    assert graph.taken_pairs == path_edges(path)
    badge = page.locator("[data-run-badge]")
    if summary.model_calls:
        assert badge.inner_text() == "Model replies scripted"
    else:  # #161: a run that never called a model claims no scripted reply
        assert badge.count() == 0
    line = _scenario(page, scenario_id, "[data-scenario-line]").inner_text()
    assert line and line == page.locator("[data-run-line]").inner_text()

    report = _scenario(page, scenario_id, "[data-report]")
    if finished.response is None:
        assert report.get_attribute("data-report") == "shed"
    else:
        assert report.get_attribute("data-report") == "report"
        answer = finished.response.report
        assert report.locator(".report__summary").inner_text() == answer.summary
        assert report.locator("[data-degradation]").inner_text() == answer.degradation_status
        assert report.locator(".report__action").count() == len(answer.recommended_actions)
    raised = [i for e in events if isinstance(e, Reconciled) for i in e.changes.approval_raised]
    marked = report.locator('.report__action[data-raised="true"]')
    assert marked.count() == len(raised)

    everything = _scenario(page, scenario_id, '[data-action="show-everything"]')
    everything.click()
    assert everything.get_attribute("aria-expanded") == "true"
    assert _scenario(page, scenario_id, ".events li").count() == len(events)

    violations = axe_violations(page)
    assert violations == [], describe(violations)
    assert watch.console_errors == []
    assert watch.failed == []


def test_skip_to_end_runs_the_scenario_and_shows_only_its_final_state(pages: GuardedPages):
    """Skip to end before Play: the stage holds still, then shows the final state; focus stays."""
    page, _ = pages.open()
    _open_chapters(page)
    page.wait_for_selector(GRAPH_READY, state="attached")
    page.locator("#scenario-self-repair-toggle").click()
    skip = _scenario(page, "self-repair", '[data-action="skip"]')
    assert skip.inner_text() == "Skip to end"

    page.evaluate(RECORD_STAGE)
    skip.click()
    page.wait_for_selector(_scenario_selector("self-repair", "[data-report]"), state="attached")

    frames = page.evaluate("() => window.stageFrames")
    streaming = [frame for frame in frames if frame["state"] == "streaming"]
    assert streaming, "the run must stream before it finishes"
    assert {(f["line"], f["rows"], f["visited"]) for f in streaming} == {
        ("Skipping to the end", 0, 0)
    }, "a skipped run reveals nothing step by step"
    final = frames[-1]
    assert final["state"] == "finished"
    assert final["line"].startswith("Report in ") and "1 self-repair round" in final["line"]
    assert final["visited"] > 0 and final["rows"] > 0
    assert graph_state(page).visits["generate_report"] == 2
    assert _scenario(page, "self-repair", "[data-scenario-line]").inner_text() == final["line"]
    assert skip.get_attribute("aria-disabled") == "true", "nothing left to skip"
    assert page.evaluate("document.activeElement.dataset.action") == "skip"


@pytest.mark.real_scripted_delays
def test_skip_to_end_mid_play_holds_the_running_stage_without_a_second_run(pages: GuardedPages):
    """Skip pressed while Play streams freezes the stage as it is; the same run then ends."""
    page, watch = pages.open()
    _open_chapters(page)
    page.wait_for_selector(GRAPH_READY, state="attached")
    page.locator("#scenario-self-repair-toggle").click()
    _scenario(page, "self-repair", '[data-action="play"]').click()
    # The first model reply takes its real scripted delay: the run is mid-stream here.
    page.wait_for_selector(
        '#stage-root [data-node="generate_report"][data-state="active"]', state="attached"
    )

    page.evaluate(RECORD_STAGE)
    skip = _scenario(page, "self-repair", '[data-action="skip"]')
    skip.click()
    page.wait_for_selector(_scenario_selector("self-repair", "[data-report]"), state="attached")

    frames = page.evaluate("() => window.stageFrames")
    streaming = [frame for frame in frames if frame["state"] == "streaming"]
    assert streaming and streaming[0]["line"] == "Skipping to the end"
    held = {(f["line"], f["rows"], f["visited"]) for f in streaming}
    assert len(held) == 1, f"the stage moved while skipping: {held}"
    ((_, rows_at_skip, visited_at_skip),) = held
    assert rows_at_skip > 0 and visited_at_skip > 0, "the run had drawn steps before the skip"
    final = frames[-1]
    assert final["state"] == "finished" and "1 self-repair round" in final["line"]
    assert final["rows"] > rows_at_skip, "the end state appears at once when the run ends"
    assert graph_state(page).visits["generate_report"] == 2
    streams = [url for url in watch.requests if url.endswith("/scenarios/self-repair/stream")]
    assert len(streams) == 1, "Skip held the run in flight; it never started another"
    assert skip.get_attribute("aria-disabled") == "true"


def test_skip_to_end_is_offered_on_every_scenario_including_the_last(pages: GuardedPages):
    """Skip to end replaces "Skip to <next>": every scenario has it, the last one too."""
    page, _ = pages.open()
    _open_chapters(page)
    last = public_catalogue().chapters[-1].scenarios[-1].id
    page.locator(f"#scenario-{last}-toggle").click()
    skip = _scenario(page, last, '[data-action="skip"]')
    assert skip.inner_text() == "Skip to end"
    assert skip.get_attribute("aria-disabled") == "false"


# --- #161: scenario panels keep their results and label honestly ---


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("scenario_id", ["cache-hit", "shed-request", "healthy-run"])
def test_the_scripted_badge_shows_only_on_runs_that_called_a_model(
    pages: GuardedPages, scenario_id: str, scheme: str
):
    """A cache hit or a shed request never calls a model, so it never claims scripted replies."""
    called_a_model = bool(summarize(stream_scenario(pages.server, scenario_id)).model_calls)
    assert called_a_model == (scenario_id == "healthy-run")
    page, _ = pages.open(color_scheme=scheme)
    _open_chapters(page)
    page.wait_for_selector(GRAPH_READY, state="attached")

    page.locator(f"#scenario-{scenario_id}-toggle").click()
    _scenario(page, scenario_id, '[data-action="play"]').click()
    wait_for_run(page)

    badge = page.locator("#stage-root [data-run-badge]")
    if called_a_model:
        assert badge.inner_text() == "Model replies scripted"
    else:
        assert badge.count() == 0
    violations = axe_violations(page)
    assert violations == [], describe(violations)


def _panel_result(page: "Page", scenario_id: str) -> dict[str, str]:
    """What an open scenario's panel shows of its last run: its line and its report."""
    return {
        "line": _scenario(page, scenario_id, "[data-scenario-line]").inner_text(),
        "report": _scenario(page, scenario_id, "[data-report]").inner_text(),
    }


def _play_to_report(page: "Page", scenario_id: str) -> None:
    """Open a scenario, press Play and wait for its report in its own panel."""
    page.locator(f"#scenario-{scenario_id}-toggle").click()
    _scenario(page, scenario_id, '[data-action="play"]').click()
    page.wait_for_selector(_scenario_selector(scenario_id, "[data-report]"), state="attached")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_each_scenario_keeps_its_last_result_after_another_plays(pages: GuardedPages, scheme: str):
    """Play A, then B: reopened, A still shows A's own summary and report, and B keeps B's."""
    page, _ = pages.open(color_scheme=scheme)
    _open_chapters(page)
    page.wait_for_selector(GRAPH_READY, state="attached")

    _play_to_report(page, "healthy-run")
    first = _panel_result(page, "healthy-run")
    _play_to_report(page, "shed-request")
    second = _panel_result(page, "shed-request")
    assert first != second

    page.locator("#scenario-healthy-run-toggle").click()
    assert _panel_result(page, "healthy-run") == first
    assert _scenario(page, "healthy-run", '[data-action="play"]').inner_text() == "Play again"
    skip = _scenario(page, "healthy-run", '[data-action="skip"]')
    assert skip.get_attribute("aria-disabled") == "true", "a kept result has nothing to skip"
    violations = axe_violations(page)
    assert violations == [], describe(violations)

    page.locator("#scenario-shed-request-toggle").click()
    assert _panel_result(page, "shed-request") == second


@pytest.mark.real_scripted_delays
def test_play_reads_play_until_the_scenarios_first_run_has_finished(pages: GuardedPages):
    """During its first run a scenario's button still says Play; once a run finished, Play again."""
    page, _ = pages.open()
    _open_chapters(page)
    page.wait_for_selector(GRAPH_READY, state="attached")
    page.locator("#scenario-self-repair-toggle").click()
    play = _scenario(page, "self-repair", '[data-action="play"]')
    assert play.inner_text() == "Play"

    play.click()
    # The first model reply takes its real scripted delay: the run is mid-stream here.
    mid_run = '#stage-root [data-node="generate_report"][data-state="active"]'
    page.wait_for_selector(mid_run, state="attached")
    assert play.inner_text() == "Play", "the first run hasn't finished yet"
    page.wait_for_selector(_scenario_selector("self-repair", "[data-report]"), state="attached")
    assert play.inner_text() == "Play again"

    play.click()
    page.wait_for_selector(mid_run, state="attached")
    assert play.inner_text() == "Play again", "a run of this scenario has finished before"
    violations = axe_violations(page)
    assert violations == [], describe(violations)


# How a button looks: what tells a visitor whether it will do anything.
BUTTON_LOOK = """(button) => {
  const style = getComputedStyle(button);
  return {color: style.color, background: style.backgroundColor,
          border: style.borderTopStyle, cursor: style.cursor};
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_skip_to_end_with_nothing_to_skip_looks_unavailable_and_keeps_focus(
    pages: GuardedPages, scheme: str
):
    """Disabled, Skip to end changes its text, fill, border and cursor, yet keeps focus."""
    page, _ = pages.open(color_scheme=scheme)
    _open_chapters(page)
    page.wait_for_selector(GRAPH_READY, state="attached")
    page.locator("#scenario-healthy-run-toggle").click()
    skip = _scenario(page, "healthy-run", '[data-action="skip"]')
    assert skip.get_attribute("aria-disabled") == "false"
    enabled = skip.evaluate(BUTTON_LOOK)

    skip.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(_scenario_selector("healthy-run", "[data-report]"), state="attached")
    page.mouse.move(0, 0)  # no hover colours on the button being measured

    assert skip.get_attribute("aria-disabled") == "true"
    assert page.evaluate("document.activeElement.dataset.action") == "skip"
    disabled = skip.evaluate(BUTTON_LOOK)
    assert disabled["cursor"] == "not-allowed"
    for part in ("color", "background", "border"):
        assert disabled[part] != enabled[part], f"a disabled Skip to end keeps its {part}"
    violations = axe_violations(page)
    assert violations == [], describe(violations)


# --- #154: hidden elements stay hidden ---
# The "Show everything" list as the visitor sees it: its computed display and its box
# (null when it renders none).
EVENTS_LIST = """(selector) => {
  const list = document.querySelector(selector);
  const box = list.getBoundingClientRect();
  return {
    display: getComputedStyle(list).display,
    box: box.width > 0 || box.height > 0 ? {width: box.width, height: box.height} : null,
  };
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_collapsed_event_list_draws_no_box_and_is_absent_from_the_a11y_tree(
    pages: GuardedPages, scheme: str
):
    """No empty box above a report: the collapsed list renders nothing, and expands in full."""
    scenario_id = "healthy-run"
    events = stream_scenario(pages.server, scenario_id)
    page, watch = pages.open(color_scheme=scheme)
    _open_chapters(page)
    page.wait_for_selector(GRAPH_READY, state="attached")
    page.locator(f"#scenario-{scenario_id}-toggle").click()
    _scenario(page, scenario_id, '[data-action="play"]').click()
    wait_for_run(page)
    page.wait_for_selector(_scenario_selector(scenario_id, "[data-report]"), state="attached")

    events_list = _scenario_selector(scenario_id, ".events")
    assert page.locator(events_list).get_attribute("hidden") is not None
    assert page.evaluate(EVENTS_LIST, events_list) == {"display": "none", "box": None}
    assert not page.locator(events_list).is_visible()
    panel = _scenario(page, scenario_id)
    assert panel.get_by_role("list", name="Every trace event").count() == 0
    assert "Every trace event" not in panel.aria_snapshot()
    violations = axe_violations(page)
    assert violations == [], describe(violations)

    _scenario(page, scenario_id, '[data-action="show-everything"]').click()
    shown = panel.get_by_role("list", name="Every trace event")
    assert shown.is_visible()
    assert shown.locator("li").count() == len(events)
    assert page.evaluate(EVENTS_LIST, events_list)["box"] is not None
    violations = axe_violations(page)
    assert violations == [], describe(violations)
    assert watch.console_errors == []
