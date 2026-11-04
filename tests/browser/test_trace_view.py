"""The live trace view in Chromium (#118): graph and waterfall drawn from a streamed run.

Runs start on the page's stage the way a play button will (harness `play`), and tests wait on
the stage's `data-run-state`, never on a timer. What the page draws is checked against the
Python summary of the same scenario run from Python, so nothing is a golden fixture.
"""

import json
from importlib import resources
from typing import Any

import pytest
from harness import GuardedPages, open_shell, play, stream_scenario, wait_for_run

from resilient_triage.trace.summary import summarize

pytestmark = pytest.mark.browser

LAYOUT: dict[str, Any] = json.loads(
    resources.files("resilient_triage.api").joinpath("static/graph-layout.json").read_text()
)
LAYOUT_EDGES = {(edge["source"], edge["target"]) for edge in LAYOUT["edges"]}

TAKEN_EDGES = """() => [...document.querySelectorAll('[data-edge][data-taken="true"]')]
  .map((edge) => ({source: edge.dataset.source, target: edge.dataset.target,
                   route: edge.dataset.route ?? null}))"""
VISITED_NODES = """() => Object.fromEntries([...document.querySelectorAll("[data-node]")]
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
