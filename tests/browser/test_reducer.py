"""The trace reducer in Chromium (#118): folds events just streamed from the live app.

Each test imports `trace-reducer.mjs` and `sse.mjs` into a real page, streams a run from the
live harness with the page's own SSE reader, and folds the events there. Python then
summarizes the very same events with `trace.summary.summarize`, the source of truth, so the JS
port is checked against it with no golden fixtures: the whole run, and every prefix of it, as
a run still streaming (a node running, no trace_finished yet) has it.
"""

import json
import re
from typing import Any

import pytest
from harness import STREAM_AND_FOLD, GuardedPages, open_shell, unique_question

from resilient_triage.graph.builder import NODE_FUNCTIONS
from resilient_triage.scenarios.catalogue import SCENARIOS
from resilient_triage.telemetry.chaos import ClusterSentinelSource
from resilient_triage.telemetry.statuspage import StatuspageSource
from resilient_triage.trace import summary as summary_module
from resilient_triage.trace.events import TraceEvent, parse_trace_event
from resilient_triage.trace.summary import summarize

pytestmark = pytest.mark.browser

SCENARIO_IDS = [scenario.id for scenario in SCENARIOS]
# Every Telemetry source the runtime reads, whose display names the reducer copies.
TELEMETRY_SOURCES = (StatuspageSource, ClusterSentinelSource)
# page.evaluate script: the constants the reducer keeps in step with Python.
REDUCER_CONSTANTS = """async () => {
  const reducer = await import("/static/trace-reducer.mjs");
  return {selfRepair: reducer.SELF_REPAIR_NODE, refine: reducer.REFINE_NODE,
          sources: reducer.SOURCE_NAMES};
}"""


def _parsed(events: list[dict[str, Any]]) -> list[TraceEvent]:
    """The events the page streamed, parsed as Python trace events."""
    return [parse_trace_event(json.dumps(event)) for event in events]


def _fold(pages: GuardedPages, source: dict[str, Any]) -> dict[str, Any]:
    """Stream a run in a fresh page and fold it with the reducer; return what the page saw."""
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)
    folded: dict[str, Any] = page.evaluate(STREAM_AND_FOLD, source)
    return folded


def _assert_summaries_match_python(folded: dict[str, Any]) -> None:
    """The reducer's summary of the run, and of every prefix of it, is exactly Python's."""
    events = _parsed(folded["events"])
    assert events[-1].kind == "trace_finished"
    assert folded["summary"] == summarize(events).model_dump(mode="json")
    assert len(folded["prefixes"]) == len(events)
    for length, prefix in enumerate(folded["prefixes"]):
        expected = summarize(events[:length]).model_dump(mode="json")
        assert prefix == expected, f"the first {length} events summarize differently"


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
    "run-deadline": "Fallback report in {t} · run deadline hit, answered outside the graph",
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
def test_each_scenario_summarizes_as_python_does_and_reads_as_its_line(
    pages: GuardedPages, scenario_id: str
):
    """One streamed run: summarize() matches Python at every prefix; summaryLine() reads it."""
    folded = _fold(pages, {"scenario": scenario_id})

    _assert_summaries_match_python(folded)
    assert _line_pattern(SUMMARY_LINES[scenario_id]).fullmatch(folded["line"]), folded["line"]


def test_a_free_play_run_summarizes_as_python_does_and_reads_as_a_report(pages: GuardedPages):
    """A free-play run folds to Python's summary at every prefix and reads as a report."""
    folded = _fold(pages, {"query": unique_question()})

    _assert_summaries_match_python(folded)
    assert _line_pattern("Report in {t}").fullmatch(folded["line"]), folded["line"]


def test_the_reducers_copied_constants_match_python(pages: GuardedPages):
    """The node names and source display names the reducer copies are Python's own."""
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    constants = page.evaluate(REDUCER_CONSTANTS)

    assert constants["selfRepair"] == summary_module._SELF_REPAIR_NODE
    assert constants["refine"] == summary_module._REFINE_NODE
    assert {constants["selfRepair"], constants["refine"]} <= set(NODE_FUNCTIONS)
    assert [source.id for source in TELEMETRY_SOURCES] == (
        pages.server.runtime.deps.telemetry.source_ids
    ), "a Telemetry source is missing from TELEMETRY_SOURCES"
    assert constants["sources"] == {source.id: source.display_name for source in TELEMETRY_SOURCES}


# --- #156: the run's clock ---
# page.evaluate script: the headline a run gets at a given wall-clock `now`, and whether the
# shell's clock should tick for it. The run started at wall time `startedAt`; its events are
# folded with the reducer, so their `t_ms` is the trace's own clock.
RUN_CLOCK = """async ({status, startedAt, now, events}) => {
  const reducer = await import("/static/trace-reducer.mjs");
  const run = {...reducer.idleRun(), status, startedAt, trace: reducer.foldTrace(events)};
  return {line: reducer.runHeadline(run, now ?? undefined).line, ticks: reducer.runTicks(run),
          summary: reducer.summaryLine(reducer.summarize(events))};
}"""
STARTED_EVENT = {
    "kind": "trace_started",
    "seq": 0,
    "t_ms": 0,
    "schema_version": 1,
    "scenario_id": "run-deadline",
    "scoped": False,
    "force_refresh": False,
}
NODE_EVENT = {"kind": "node_started", "seq": 1, "t_ms": 3, "node": "analyze_query"}


def _run_clock(pages: GuardedPages, **run: Any) -> dict[str, Any]:
    """The headline and ticking of a run built in a fresh page (see RUN_CLOCK)."""
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)
    result: dict[str, Any] = page.evaluate(RUN_CLOCK, run)
    return result


def test_a_streaming_runs_clock_counts_wall_time_since_it_started(pages: GuardedPages):
    """Waiting on something slow, the headline keeps counting from when the run started."""
    clock = _run_clock(
        pages, status="streaming", startedAt=10_000, now=11_800, events=[STARTED_EVENT, NODE_EVENT]
    )

    assert clock["line"] == "Running, 1.8 s in"
    assert clock["ticks"] is True


def test_a_streaming_runs_clock_never_reads_behind_its_last_event(pages: GuardedPages):
    """A `now` sampled before the latest event landed still shows at least that event's time."""
    clock = _run_clock(
        pages, status="streaming", startedAt=10_000, now=10_001, events=[STARTED_EVENT, NODE_EVENT]
    )

    assert clock["line"] == "Running, 3 ms in"


def test_a_streaming_run_without_a_now_reads_its_last_event(pages: GuardedPages):
    """No wall clock given: the headline reads the trace's own last timestamp."""
    clock = _run_clock(
        pages, status="streaming", startedAt=10_000, now=None, events=[STARTED_EVENT, NODE_EVENT]
    )

    assert clock["line"] == "Running, 3 ms in"


def test_an_ended_run_ignores_the_wall_clock_and_stops_ticking(pages: GuardedPages):
    """Once a run ends, its line is the trace summary's own duration and the clock is idle."""
    finished_event = {
        "kind": "trace_finished",
        "seq": 1,
        "t_ms": 3012,
        "outcome": "shed",
        "response": None,
    }

    finished = _run_clock(
        pages,
        status="finished",
        startedAt=10_000,
        now=99_000,
        events=[STARTED_EVENT, finished_event],
    )
    idle = _run_clock(pages, status="idle", startedAt=None, now=99_000, events=[])

    assert finished["line"] == finished["summary"]
    assert "3.0 s" in finished["line"]
    assert finished["ticks"] is False
    assert idle["ticks"] is False
