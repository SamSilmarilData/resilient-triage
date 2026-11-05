"""The trace reducer in Chromium (#118): folds events just streamed from the live app.

Each test imports `trace-reducer.mjs` and `sse.mjs` into a real page, streams a run from the
live harness with the page's own SSE reader, and folds the events there. Python then
summarizes the very same events with `trace.summary.summarize`, the source of truth, so the JS
port is checked against it with no golden fixtures.
"""

import json
import re
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
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)
    folded: dict[str, Any] = page.evaluate(STREAM_AND_FOLD, source)
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


# What summaryLine() says for each scenario, from the scenario's intent in the catalogue:
# the triage outcome plus its modifiers. `{t}` stands for the run's total time.
SUMMARY_LINES = {
    "healthy-run": "Report in {t}",
    "cache-hit": "Answered from cache in {t}",
    "model-failover": "Report in {t} · 1 model failover",
    "self-repair": "Report in {t} · 1 self-repair round",
    "refine": "Report in {t} · 1 refine round",
    "refine-then-provider-failure": (
        "Report in {t} · 1 refine round · 1 model failover · no model answered"
    ),
    "self-repair-exhausted": "Fallback report in {t} · 1 self-repair round",
    "reconciliation": "Report in {t} · 1 action held for approval",
    "unavailable-source": "Report in {t} · Statuspage unavailable",
    "tripped-breaker": "Report in {t} · Statuspage breaker open",
    "run-deadline": "Fallback report in {t} · run deadline hit",
    "shed-request": "Shed after {t}, before the graph ran",
    "everything-down": (
        "Fallback report in {t} · Statuspage breaker open · Cluster Sentinel breaker open"
        " · 1 model failover · no model answered"
    ),
}
DURATION = r"(?:under 1 ms|[1-9]\d* ms|\d+\.\d s)"


def _line_pattern(template: str) -> re.Pattern[str]:
    """A template from SUMMARY_LINES as a full-match pattern, `{t}` matching a duration."""
    return re.compile(re.escape(template).replace(re.escape("{t}"), DURATION))


def test_every_scenario_has_an_expected_summary_line():
    """SUMMARY_LINES covers exactly the catalogue, so a new scenario gets a line checked."""
    assert set(SUMMARY_LINES) == set(SCENARIO_IDS)


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_summary_line_names_the_outcome_and_its_modifiers(pages: GuardedPages, scenario_id: str):
    """summaryLine() reads the summary of each scenario's real run, never the scenario id."""
    folded = _fold(pages, {"scenario": scenario_id})

    assert _line_pattern(SUMMARY_LINES[scenario_id]).fullmatch(folded["line"]), folded["line"]


def test_summary_line_of_a_free_play_run(pages: GuardedPages):
    """A plain free-play run reads as a report and its time."""
    folded = _fold(pages, {"query": "GitHub Actions workflows failing with 503s"})

    assert _line_pattern("Report in {t}").fullmatch(folded["line"]), folded["line"]
