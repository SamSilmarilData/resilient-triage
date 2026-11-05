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
    assert page.locator("[data-run-badge]").inner_text() == "Model replies scripted"
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
