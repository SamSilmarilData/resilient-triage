"""The trace reducer in Chromium (#118): folds events just streamed from the live app.

Each test imports `trace-reducer.mjs` and `sse.mjs` into a real page, streams a run from the
live harness with the page's own SSE reader, and folds the events there. Python then
summarizes the very same events with `trace.summary.summarize`, the source of truth, so the JS
port is checked against it with no golden fixtures.
"""

import json
from typing import Any

import pytest
from harness import STREAM_AND_FOLD, GuardedPages, open_shell

from resilient_triage.scenarios.catalogue import SCENARIOS
from resilient_triage.trace.events import parse_trace_event
from resilient_triage.trace.summary import TraceSummary, summarize

pytestmark = pytest.mark.browser

SCENARIO_IDS = [scenario.id for scenario in SCENARIOS]


def _python_summary(events: list[dict[str, Any]]) -> TraceSummary:
    """Python's summary of the events the page streamed."""
    return summarize([parse_trace_event(json.dumps(event)) for event in events])


def _fold(pages: GuardedPages, source: dict[str, Any]) -> dict[str, Any]:
    """Stream a run in a fresh page and fold it with the reducer; return what the page saw."""
    page, watch = pages.open()
    open_shell(page, wait_for_requests=False)
    folded: dict[str, Any] = page.evaluate(STREAM_AND_FOLD, source)
    assert watch.page_errors == []
    return folded


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_the_reducer_summary_matches_python_for_every_scenario(
    pages: GuardedPages, scenario_id: str
):
    """summarize() in JS gives exactly Python's TraceSummary of the same streamed events."""
    folded = _fold(pages, {"scenario": scenario_id})

    events = folded["events"]
    assert events[-1]["kind"] == "trace_finished"
    assert folded["summary"] == _python_summary(events).model_dump(mode="json")


def test_the_reducer_summary_matches_python_on_free_play(pages: GuardedPages):
    """A free-play run folds to Python's summary too."""
    folded = _fold(pages, {"query": "GitHub Actions workflows failing with 503s"})

    events = folded["events"]
    assert events[-1]["kind"] == "trace_finished"
    assert folded["summary"] == _python_summary(events).model_dump(mode="json")
