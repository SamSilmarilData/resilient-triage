"""The live trace view in Chromium (#118): graph and waterfall drawn from a streamed run.

Runs start on the page's stage the way a play button will (harness `play`), and tests wait on
the stage's `data-run-state`, never on a timer. What the page draws is checked against the
Python summary of the same scenario run from Python, so nothing is a golden fixture.
"""

import json
from importlib import resources
from typing import TYPE_CHECKING, Any

import pytest
from harness import GuardedPages, open_shell, play, stream_scenario, wait_for_run

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

LAYOUT: dict[str, Any] = json.loads(
    resources.files("resilient_triage.api").joinpath("static/graph-layout.json").read_text()
)
LAYOUT_EDGES = {(edge["source"], edge["target"]) for edge in LAYOUT["edges"]}

TAKEN_EDGES = """() => [...document.querySelectorAll('[data-edge][data-taken="true"]')]
  .map((edge) => ({source: edge.dataset.source, target: edge.dataset.target,
                   route: edge.dataset.route ?? null}))"""
# Graph nodes only: __start__ and __end__ are the graph's terminals, not steps of the path.
VISITED_NODES = """() => Object.fromEntries([...document.querySelectorAll("[data-node]")]
  .filter((node) => !node.dataset.node.startsWith("__"))
  .map((node) => [node.dataset.node, Number(node.dataset.visits)]))"""


def _path_edges(path: list[str]) -> set[tuple[str, str]]:
    """The layout edges a node path walks, from __start__ to __end__."""
    walk = ["__start__", *path, "__end__"]
    return {pair for pair in zip(walk, walk[1:], strict=False) if pair in LAYOUT_EDGES}


@pytest.mark.parametrize("scenario_id", ["self-repair", "everything-down"])
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

    visits = page.evaluate(VISITED_NODES)
    assert {node for node, count in visits.items() if count} == set(path)
    assert visits["generate_report"] == path.count("generate_report")
    taken = page.evaluate(TAKEN_EDGES)
    assert {(edge["source"], edge["target"]) for edge in taken} == _path_edges(path)
    assert {edge["route"] for edge in taken if edge["route"]} == set(summary.routes)
    assert watch.page_errors == []
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


def test_free_play_model_calls_show_the_model_that_answered(pages: GuardedPages):
    """Free play is no scenario: its model row names Completion.model, with no scripted mark."""
    page, _ = pages.open()
    open_shell(page)

    play(page, query="GitHub Actions workflows failing with 503s")
    wait_for_run(page)

    models = [row for row in _bars(page) if row["kind"] == "model"]
    assert [row["label"] for row in models] == [settings.groq_model]
    assert "scripted reply" not in models[0]["text"]
