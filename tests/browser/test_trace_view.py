"""The live trace view in Chromium (#118): graph and waterfall drawn from a streamed run.

Runs start on the page's stage the way a play button will (harness `play`), and tests wait on
the stage's `data-run-state`, never on a timer. What the page draws is checked against the
Python summary of the same scenario run from Python, so nothing is a golden fixture.
"""

import json
from typing import TYPE_CHECKING, Any

import pytest
from harness import (
    GRAPH_READY,
    LAYOUT_EDGES,
    PAGE_WIDTHS,
    GuardedPages,
    axe_violations,
    describe,
    graph_state,
    open_shell,
    path_edges,
    play,
    run_selector,
    stream_scenario,
    wait_for_run,
)

from resilient_triage.config import settings
from resilient_triage.trace.events import (
    ModelCall,
    NodeFinished,
    NodeStarted,
    ScenarioSetup,
    TelemetryRead,
    TraceEvent,
    parse_trace_event,
)
from resilient_triage.trace.summary import summarize

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

STAGE_EVENTS = """async () => (await import("/static/app.mjs")).stage.view.trace.events"""
# Each waterfall row with its bar's left edge and width as fractions of its track.
WATERFALL_ROWS = """() => [...document.querySelectorAll(".waterfall__row")].map((row) => {
  const track = row.querySelector(".waterfall__track").getBoundingClientRect();
  const bar = row.querySelector(".waterfall__bar").getBoundingClientRect();
  return {kind: row.dataset.kind, label: row.querySelector(".waterfall__name").textContent,
          text: row.textContent, left: (bar.left - track.left) / track.width,
          width: bar.width / track.width};
})"""
# The zero line's position as a fraction of the track.
ZERO_LINE = """() => {
  const track = document.querySelector(".waterfall__track").getBoundingClientRect();
  const zero = document.querySelector(".waterfall__zero").getBoundingClientRect();
  return (zero.left - track.left) / track.width;
}"""


@pytest.mark.parametrize("scenario_id", ["self-repair", "everything-down", "run-deadline"])
def test_a_played_scenario_lights_its_visited_nodes_and_taken_edges(
    pages: GuardedPages, scenario_id: str
):
    """After a run, the graph shows exactly the summary's node path and routes."""
    summary = summarize(stream_scenario(pages.server, scenario_id))
    path = [visit.node for visit in summary.nodes]
    page, watch = pages.open()
    open_shell(page)

    play(page, scenario=scenario_id)
    wait_for_run(page)

    graph = graph_state(page)
    assert graph.visited == set(path)
    assert graph.visits["generate_report"] == path.count("generate_report")
    assert graph.taken_pairs == path_edges(path)
    assert graph.taken_routes == set(summary.routes)
    end_state = page.locator('[data-node="__end__"]').get_attribute("data-state")
    assert (end_state == "visited") is ((path[-1], "__end__") in LAYOUT_EDGES)
    assert watch.console_errors == []


def _stage_events(page: "Page") -> list[TraceEvent]:
    """The events the stage's run folded, parsed as Python trace events."""
    raw = page.evaluate(STAGE_EVENTS)
    return [parse_trace_event(json.dumps(event)) for event in raw]


def _bars(page: "Page") -> list[dict[str, Any]]:
    """Every waterfall row: its data attributes, label text and bar geometry in its track."""
    rows: list[dict[str, Any]] = page.evaluate(WATERFALL_ROWS)
    return rows


@pytest.mark.real_scripted_delays
def test_waterfall_bars_keep_the_runs_real_proportions(pages: GuardedPages):
    """Each node's bar spans its real start and duration on one clock from t = 0."""
    page, _ = pages.open()
    open_shell(page)

    play(page, scenario="healthy-run")
    wait_for_run(page)

    events = _stage_events(page)
    summary = summarize(events)
    total = summary.timings.total_ms
    assert total > 500, "the scripted reply's real delay should dominate the run"
    starts = {e.node: e.t_ms for e in events if isinstance(e, NodeStarted)}
    nodes = {row["label"]: row for row in _bars(page) if row["kind"] == "node"}
    assert set(nodes) == {visit.node for visit in summary.nodes}
    for visit in summary.nodes:
        bar = nodes[visit.node]
        assert visit.duration_ms is not None
        assert bar["width"] == pytest.approx(visit.duration_ms / total, abs=0.01)
        assert bar["left"] == pytest.approx(starts[visit.node] / total, abs=0.01)


def test_scenario_setup_draws_before_t_zero(pages: GuardedPages):
    """Setup steps and setup reads sit left of the zero line; the run itself sits right of it."""
    page, _ = pages.open()
    open_shell(page)

    play(page, scenario="tripped-breaker")
    wait_for_run(page)

    events = _stage_events(page)
    setup_events = [
        e
        for e in events
        if isinstance(e, ScenarioSetup) or (isinstance(e, TelemetryRead) and e.phase == "setup")
    ]
    rows = _bars(page)
    zero = page.evaluate(ZERO_LINE)
    setup = [row for row in rows if row["kind"] == "setup"]
    assert len(setup) == len(setup_events) > 0
    assert zero > 0
    for row in setup:
        assert row["left"] + row["width"] <= zero + 0.001
    for row in rows:
        if row["kind"] != "setup":
            assert row["left"] >= zero - 0.001


def test_scenario_model_calls_are_marked_scripted(pages: GuardedPages):
    """On a scenario run every model call is marked "scripted reply"; validate shows confidence."""
    page, _ = pages.open()
    open_shell(page)

    play(page, scenario="model-failover")
    wait_for_run(page)

    calls = [e for e in _stage_events(page) if isinstance(e, ModelCall)]
    models = [row for row in _bars(page) if row["kind"] == "model"]
    assert [row["label"] for row in models] == [call.model for call in calls]
    assert all("scripted reply" in row["text"] for row in models)
    (validate,) = [row for row in _bars(page) if row["label"] == "validate_report"]
    finished = [e for e in _stage_events(page) if isinstance(e, NodeFinished)]
    (confidence,) = [e.confidence for e in finished if e.node == "validate_report"]
    assert confidence is not None
    assert f"confidence {confidence:.2f}" in validate["text"]


@pytest.mark.parametrize(
    ("scenario_id", "line_start"),
    [
        ("shed-request", "Shed after "),
        ("run-deadline", "Fallback report in "),
        ("self-repair-exhausted", "Fallback report in "),
        ("cache-hit", "Answered from cache in "),
    ],
)
def test_every_outcome_reaches_the_finished_state(
    pages: GuardedPages, scenario_id: str, line_start: str
):
    """Shed, fallback and cache-hit runs all end in trace_finished with their summary line."""
    page, watch = pages.open()
    open_shell(page)

    play(page, scenario=scenario_id)
    wait_for_run(page)

    assert page.locator("[data-run-line]").inner_text().startswith(line_start)
    assert page.locator(".run button").count() == 0
    assert watch.failed == []


def _sse(events: list[TraceEvent]) -> str:
    """Events framed as the server frames them."""
    return "".join(f"event: {e.kind}\ndata: {e.model_dump_json()}\n\n" for e in events)


def test_a_stream_that_closes_early_is_lost_then_replays(pages: GuardedPages):
    """A close before trace_finished keeps the partial trace, dimmed, with Replay."""
    events = stream_scenario(pages.server, "self-repair")
    cut = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, NodeStarted) and e.node == "validate_report"
    )
    page, _ = pages.open()
    open_shell(page)
    url = "**/scenarios/self-repair/stream"
    page.route(
        url,
        lambda route: route.fulfill(
            status=200, content_type="text/event-stream", body=_sse(events[:cut])
        ),
    )

    play(page, scenario="self-repair")
    wait_for_run(page, "lost")

    assert page.locator("[data-run-line]").inner_text() == "Connection lost"
    assert page.locator('.run[data-dimmed="true"]').count() == 1
    visits = graph_state(page).visits
    assert visits["generate_report"] == 1 and visits["validate_report"] == 0
    assert page.locator(".waterfall__row").count() > 0, "the partial trace must stay drawn"

    page.unroute(url)
    page.get_by_role("button", name="Replay").click()
    wait_for_run(page)
    assert graph_state(page).visits["validate_report"] == 2


# A stream source the test feeds by hand: window.feed(message) yields one stream message.
MANUAL_SOURCE = """async () => {
  const {stage} = await import("/static/app.mjs");
  const queue = [];
  let wake = null;
  window.feed = (message) => { queue.push(message); wake?.(); };
  stage.play(async function* () {
    for (;;) {
      while (queue.length) yield queue.shift();
      await new Promise((resolve) => { wake = resolve; });
    }
  });
}"""
STARTED = {
    "kind": "event",
    "event": {
        "seq": 0,
        "t_ms": 0,
        "kind": "trace_started",
        "schema_version": 1,
        "scenario_id": None,
        "scoped": False,
        "force_refresh": False,
    },
}


def test_a_silent_stream_is_lost_after_30_seconds_and_pings_keep_it_alive(pages: GuardedPages):
    """No event or ping for 30 s ends the run as lost; a ping restarts that clock."""
    page, _ = pages.open()
    page.clock.install()
    open_shell(page, wait_for_requests=False)
    page.wait_for_selector(GRAPH_READY, state="attached")
    # From here the page's clock moves only when the test moves it.
    page.clock.pause_at(page.evaluate("Date.now()") + 1_000)
    page.evaluate(MANUAL_SOURCE)
    page.evaluate("(message) => window.feed(message)", STARTED)
    wait_for_run(page, "streaming")

    page.clock.run_for(20_000)
    page.evaluate("() => window.feed({kind: 'ping'})")
    page.clock.run_for(20_000)
    assert page.locator(run_selector("streaming")).count() == 1, "a ping must keep it alive"

    page.clock.run_for(10_500)
    wait_for_run(page, "lost", timeout_ms=5_000)
    assert page.locator("[data-run-line]").inner_text() == "Connection lost"


# page.evaluate script: feed stream messages to MANUAL_SOURCE's run, then wait until the stage's
# controller has folded them all and two frames have passed, so any redraw they cause is drawn.
FEED_AND_SETTLE = """async (messages) => {
  const {stage} = await import("/static/app.mjs");
  const target = stage.view.trace.events.length + messages.length;
  for (const message of messages) window.feed(message);
  while (stage.view.trace.events.length < target) await new Promise((r) => setTimeout(r, 0));
  for (let i = 0; i < 2; i++) await new Promise((r) => requestAnimationFrame(r));
}"""
SKIP_TO_END = """async () => (await import("/static/app.mjs")).stage.skipToEnd()"""


def _messages(events: list[TraceEvent]) -> list[dict[str, Any]]:
    """Trace events as MANUAL_SOURCE stream messages."""
    return [{"kind": "event", "event": json.loads(e.model_dump_json())} for e in events]


def test_skip_to_end_mid_run_holds_the_stage_until_trace_finished(pages: GuardedPages):
    """Skip to end mid-run: later events draw nothing until trace_finished shows the end."""
    events = stream_scenario(pages.server, "self-repair")
    cut = next(i for i, e in enumerate(events) if isinstance(e, NodeFinished)) + 1
    page, _ = pages.open()
    open_shell(page)
    page.wait_for_selector(GRAPH_READY, state="attached")
    page.evaluate(MANUAL_SOURCE)
    page.evaluate(FEED_AND_SETTLE, _messages(events[:cut]))
    wait_for_run(page, "streaming")
    rows_at_skip = page.locator(".waterfall__row").count()
    visits_at_skip = graph_state(page).visits
    assert rows_at_skip > 0

    page.evaluate(SKIP_TO_END)
    page.evaluate(FEED_AND_SETTLE, _messages(events[cut:-1]))
    assert page.locator("[data-run-line]").inner_text() == "Skipping to the end"
    assert page.locator(".waterfall__row").count() == rows_at_skip, "no step-by-step reveal"
    assert graph_state(page).visits == visits_at_skip
    assert page.locator('[role="status"]').inner_text() == ""

    page.evaluate(FEED_AND_SETTLE, _messages(events[-1:]))
    wait_for_run(page)
    summary = summarize(events)
    graph = graph_state(page)
    assert graph.visits["generate_report"] == 2
    assert graph.visited == {visit.node for visit in summary.nodes}
    line = page.locator("[data-run-line]").inner_text()
    assert line.startswith("Report in ") and "1 self-repair round" in line
    assert page.locator('[role="status"]').inner_text() == f"Run finished: {line}"


def test_a_refused_stream_shows_its_error_with_retry(pages: GuardedPages):
    """A 404 before the stream starts is an inline error with Retry, not a lost run."""
    page, watch = pages.open()
    open_shell(page)

    play(page, scenario="no-such-scenario")
    wait_for_run(page, "error")

    assert page.locator("[data-run-line]").inner_text() == "No scenario 'no-such-scenario'."
    assert page.get_by_role("button", name="Retry").count() == 1
    assert [failure.split(" ")[0] for failure in watch.failed] == ["404"]


def test_an_unreachable_server_shows_an_error_with_retry(pages: GuardedPages):
    """A network failure before any event says the server couldn't be reached."""
    page, watch = pages.open()
    open_shell(page)
    page.route("**/scenarios/healthy-run/stream", lambda route: route.abort("connectionfailed"))

    play(page, scenario="healthy-run")
    wait_for_run(page, "error")

    assert page.locator("[data-run-line]").inner_text().startswith("Couldn't reach the server")
    assert page.get_by_role("button", name="Retry").count() == 1
    assert len(watch.failed) == 1


def test_free_play_model_calls_show_the_model_that_answered(pages: GuardedPages):
    """Free play is no scenario: its model row names Completion.model, with no scripted mark."""
    page, _ = pages.open()
    open_shell(page)

    play(page, query="GitHub Actions workflows failing with 503s")
    wait_for_run(page)

    models = [row for row in _bars(page) if row["kind"] == "model"]
    assert [row["label"] for row in models] == [settings.groq_model]
    assert "scripted reply" not in models[0]["text"]


# The transition on a graph node's box, and the animations that last long enough to see, while
# a node is active. The base reduced-motion rule leaves 0.01 ms transitions behind: not motion.
MOTION = """() => ({
  transition: getComputedStyle(document.querySelector(".graph__box")).transitionDuration,
  animations: document.getAnimations()
    .filter((animation) => animation.effect.getTiming().duration > 1).length,
})"""


@pytest.mark.real_scripted_delays
@pytest.mark.parametrize(("reduced_motion", "moves"), [("no-preference", True), ("reduce", False)])
def test_motion_is_css_only_and_off_under_reduced_motion(
    pages: GuardedPages, reduced_motion: str, moves: bool
):
    """Lit nodes fade and the active node pulses in CSS; under reduced motion nothing moves."""
    page, _ = pages.open(reduced_motion=reduced_motion)
    open_shell(page)

    play(page, scenario="healthy-run")
    page.wait_for_selector('[data-node="generate_report"][data-state="active"]', state="attached")
    motion = page.evaluate(MOTION)
    wait_for_run(page)

    seconds = float(motion["transition"].split(",")[0].removesuffix("s"))
    assert (seconds >= 0.1) is moves, motion
    assert (motion["animations"] > 0) is moves, motion


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_finished_run_has_no_serious_axe_violations(pages: GuardedPages, color_scheme: str):
    """The lit graph and the waterfall pass axe-core in both themes."""
    page, _ = pages.open(color_scheme=color_scheme)
    open_shell(page)

    play(page, scenario="everything-down")
    wait_for_run(page)

    violations = axe_violations(page)
    assert violations == [], describe(violations)


def test_a_finished_run_fits_phone_width(pages: GuardedPages):
    """At 375 px the graph sits inline above the waterfall and nothing scrolls sideways."""
    page, _ = pages.open(viewport={"width": 375, "height": 812})
    open_shell(page)

    play(page, scenario="everything-down")
    wait_for_run(page)

    widths = page.evaluate(PAGE_WIDTHS)
    assert widths[0] <= widths[1], f"page is {widths[0]} px wide in a {widths[1]} px viewport"
    graph = page.locator(".graph").bounding_box()
    waterfall = page.locator(".waterfall").bounding_box()
    assert graph is not None and waterfall is not None
    assert graph["y"] + graph["height"] <= waterfall["y"]
    assert graph["width"] >= 340
