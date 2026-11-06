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
from harness import STREAM_AND_FOLD, GuardedPages, open_shell, stream_scenario, unique_question

from resilient_triage.graph.builder import NODE_FUNCTIONS
from resilient_triage.scenarios.catalogue import SCENARIOS, public_catalogue
from resilient_triage.telemetry.chaos import ClusterSentinelSource
from resilient_triage.telemetry.statuspage import StatuspageSource
from resilient_triage.trace import summary as summary_module
from resilient_triage.trace.events import (
    ScenarioSetup,
    TelemetryRead,
    TraceEvent,
    parse_trace_event,
)
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


# --- #159: readable timeline ---------------------------------------------------------------
# page.evaluate script: fold events with the reducer and return its waterfall view.
WATERFALL_OF = """async (events) => {
  const reducer = await import("/static/trace-reducer.mjs");
  return reducer.waterfallView(reducer.foldTrace(events));
}"""
# The same, for a track `trackPx` wide.
WATERFALL_ON_TRACK = """async ([events, trackPx]) => {
  const reducer = await import("/static/trace-reducer.mjs");
  return reducer.waterfallView(reducer.foldTrace(events), trackPx);
}"""
# page.evaluate script: whether the axis's zero and end labels would meet on a track this wide.
LABELS_COLLIDE = """async ([zeroPct, endLabel, trackPx]) =>
  (await import("/static/trace-reducer.mjs")).axisLabelsCollide(zeroPct, endLabel, trackPx)"""
SETUP_SCENARIOS = [scenario.id for scenario in SCENARIOS if scenario.setup is not None]


def _waterfall(page: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    """The reducer's waterfall view of these wire events."""
    view: dict[str, Any] = page.evaluate(WATERFALL_OF, events)
    return view


def _wire(events: list[TraceEvent]) -> list[dict[str, Any]]:
    """Trace events as the page receives them."""
    return [json.loads(event.model_dump_json()) for event in events]


def _long_setup_run(setup_ms: float, run_ms: float) -> list[dict[str, Any]]:
    """A hand-made run: one setup step whose one read took setup_ms, then run_ms of run."""
    read = {"source": "statuspage", "outcome": "failed", "attempts": 3, "signals": 0}
    breaker = {"breaker_before": "closed", "breaker_after": "open"}
    setup_read = {"kind": "telemetry_read", "seq": 0, "t_ms": 0, "phase": "setup"}
    step = {"kind": "scenario_setup", "seq": 1, "t_ms": 0, "step": "fail_source", "count": 3}
    return [
        {**setup_read, "duration_ms": setup_ms, **read, **breaker},
        {**step, "source": "statuspage"},
        {"kind": "trace_started", "seq": 2, "t_ms": 0, "schema_version": 1},
        {"kind": "node_started", "seq": 3, "t_ms": 0, "node": "analyze_query"},
        {"kind": "node_finished", "seq": 4, "t_ms": run_ms, "node": "analyze_query"}
        | {"status": "ok"},
    ]


@pytest.mark.parametrize("scenario_id", SETUP_SCENARIOS)
def test_setup_steps_are_listed_before_the_reads_they_caused(pages: GuardedPages, scenario_id: str):
    """Each setup step's row comes first, then the setup reads it made, indented under it."""
    events = stream_scenario(pages.server, scenario_id)
    expected: list[str] = []
    reads: list[str] = []
    for event in events:
        if isinstance(event, TelemetryRead) and event.phase == "setup":
            reads.append(str(event.seq))
        elif isinstance(event, ScenarioSetup):
            expected += [str(event.seq), *reads]
            reads = []
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    rows = [row for row in _waterfall(page, _wire(events))["rows"] if row["kind"] == "setup"]

    assert [row["key"] for row in rows] == expected
    steps = {str(e.seq) for e in events if isinstance(e, ScenarioSetup)}
    assert all(row["depth"] == (0 if row["key"] in steps else 1) for row in rows)


@pytest.mark.parametrize(("setup_ms", "run_ms"), [(5_000, 100), (200_000, 40), (300, 0.2)])
def test_a_long_setup_leaves_the_run_three_quarters_of_the_width(
    pages: GuardedPages, setup_ms: float, run_ms: float
):
    """Setup draws on its own compressed scale, at most a quarter: the run keeps the rest."""
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    view = _waterfall(page, _long_setup_run(setup_ms, run_ms))

    assert view["zeroPct"] == pytest.approx(25)
    setup = [row for row in view["rows"] if row["kind"] == "setup"]
    (node,) = [row for row in view["rows"] if row["kind"] == "node"]
    for row in setup:
        assert 0 <= row["leftPct"] and row["leftPct"] + row["widthPct"] <= 25 + 1e-9
    assert max(row["leftPct"] + row["widthPct"] for row in setup) == pytest.approx(25)
    assert node["leftPct"] == pytest.approx(25)
    assert node["leftPct"] + node["widthPct"] == pytest.approx(100)


def test_a_short_setup_keeps_its_real_proportion(pages: GuardedPages):
    """Setup shorter than a quarter of the axis stays on the run's clock, as before."""
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    view = _waterfall(page, _long_setup_run(200, 800))

    assert view["zeroPct"] == pytest.approx(20)
    (node,) = [row for row in view["rows"] if row["kind"] == "node"]
    assert node["leftPct"] == pytest.approx(20)
    assert node["widthPct"] == pytest.approx(80)


@pytest.mark.parametrize(
    ("zero_pct", "end_label", "track_px", "collide"),
    [
        # The "0" label sits 4 to ~11 px right of the zero line; "under 1 ms" is ~67 px wide.
        (25, "under 1 ms", 400, False),
        (25, "under 1 ms", 100, True),
        (90, "423 ms", 400, True),
        (0, "1.8 s", 60, False),
        (0, "1.8 s", 40, True),
    ],
)
def test_axis_labels_collide_when_their_boxes_would_meet(
    pages: GuardedPages, zero_pct: float, end_label: str, track_px: float, collide: bool
):
    """The zero tick's label and the end label are checked as boxes on a track that wide."""
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    assert page.evaluate(LABELS_COLLIDE, [zero_pct, end_label, track_px]) is collide


def test_a_run_whose_labels_would_meet_hides_the_zero_label(pages: GuardedPages):
    """On a track too narrow for both labels, the view hides the zero label, never the end."""
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)
    events = _long_setup_run(5_000, 100)

    assert page.evaluate(WATERFALL_ON_TRACK, [events, 400])["showZeroLabel"] is True
    narrow = page.evaluate(WATERFALL_ON_TRACK, [events, 60])
    assert narrow["showZeroLabel"] is False
    assert narrow["endLabel"] == "100 ms"


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_every_scenario_shows_its_zero_label_and_keeps_most_of_the_axis(
    pages: GuardedPages, scenario_id: str
):
    """No catalogued run would collide its labels, and none gives setup over a quarter."""
    events = stream_scenario(pages.server, scenario_id)
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    view = _waterfall(page, _wire(events))

    assert view["showZeroLabel"] is True
    assert view["zeroPct"] <= 25


def test_cache_rows_say_when_the_cache_ran_in_memory(pages: GuardedPages):
    """Redis down puts the cache in memory, and its rows say so; Redis search says nothing."""
    events = _wire(stream_scenario(pages.server, "everything-down"))
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    rows = _waterfall(page, events)["rows"]
    cache = [row for row in rows if row["kind"] in ("cache", "store")]
    assert {row["kind"] for row in cache} == {"cache", "store"}
    assert all(row["detailParts"][-1] == "in memory" for row in cache)
    assert all(row["detail"] == " · ".join(row["detailParts"]) for row in cache)

    on_redis = [{**e, "backend": "redisearch"} if "backend" in e else e for e in events]
    cache = [row for row in _waterfall(page, on_redis)["rows"] if row["kind"] in ("cache", "store")]
    assert cache and all("in memory" not in row["detail"] for row in cache)


# page.evaluate script: the details of a run's cache rows, measured against the backend the
# page counts as normal for it (the scenario's setup, or free play's GET /health).
CACHE_DETAILS = """async ([events, catalogue, health]) => {
  const reducer = await import("/static/trace-reducer.mjs");
  const trace = reducer.foldTrace(events);
  const normal = reducer.normalCacheBackend(trace, catalogue, health);
  return reducer.waterfallView(trace, undefined, normal).rows
    .filter((row) => row.kind === "cache" || row.kind === "store")
    .map((row) => row.detailParts);
}"""


@pytest.mark.parametrize(
    ("scenario_id", "named"), [("everything-down", True), ("healthy-run", False)]
)
def test_a_scenario_names_its_cache_backend_only_when_its_setup_takes_redis_down(
    pages: GuardedPages, scenario_id: str, named: bool
):
    """Every scenario's cache is private and in memory; only Redis down makes that news."""
    events = _wire(stream_scenario(pages.server, scenario_id))
    catalogue = public_catalogue().model_dump(mode="json")
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    details = page.evaluate(CACHE_DETAILS, [events, catalogue, "loading"])

    assert details
    assert all((parts[-1] == "in memory") is named for parts in details), details


@pytest.mark.parametrize(
    ("health", "named"),
    [
        ({"cache_driver": "in_memory"}, False),
        ({"cache_driver": "redisearch"}, True),
        ("failed", False),
    ],
    ids=["normal-in-memory", "normal-redis", "health-unknown"],
)
def test_free_play_names_its_cache_backend_only_when_it_isnt_the_deployments(
    pages: GuardedPages, health: Any, named: bool
):
    """Free play's normal backend is the one GET /health reports; unknown, rows name none."""
    events = _wire(stream_scenario(pages.server, "everything-down"))
    events = [{**e, "scenario_id": None} if e["kind"] == "trace_started" else e for e in events]
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    details = page.evaluate(CACHE_DETAILS, [events, "loading", health])

    assert details
    assert all((parts[-1] == "in memory") is named for parts in details), details


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


# --- #158: the stage names its scenario ---
# page.evaluate script: what the stage says about the scenario of each run (a status and the
# events folded so far), given the catalogue (or "loading"/"failed").
STAGE_SCENARIO = """async ({catalogue, runs}) => {
  const reducer = await import("/static/trace-reducer.mjs");
  return runs.map(({status, events}) => reducer.stageScenario(
    catalogue, {...reducer.idleRun(), status, trace: reducer.foldTrace(events)}));
}"""


def test_the_stage_names_its_runs_scenario_and_offers_a_way_back_once_finished(
    pages: GuardedPages,
):
    """The title comes from the catalogue by the trace's scenario id; the way back to the
    scenario's card and report is offered only once the run has finished."""
    catalogue = public_catalogue()
    last = catalogue.chapters[-1].scenarios[-1]
    started = {**STARTED_EVENT, "scenario_id": last.id}
    free_play = {**STARTED_EVENT, "scenario_id": None}
    uncatalogued = {**STARTED_EVENT, "scenario_id": "not-catalogued"}
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    seen = page.evaluate(
        STAGE_SCENARIO,
        {
            "catalogue": catalogue.model_dump(mode="json"),
            "runs": [
                {"status": "idle", "events": []},
                {"status": "streaming", "events": []},
                {"status": "streaming", "events": [started, NODE_EVENT]},
                {"status": "finished", "events": [started, NODE_EVENT]},
                {"status": "lost", "events": [started]},
                {"status": "finished", "events": [free_play]},
                {"status": "finished", "events": [uncatalogued]},
            ],
        },
    )
    loading = page.evaluate(
        STAGE_SCENARIO,
        {"catalogue": "loading", "runs": [{"status": "finished", "events": [started]}]},
    )

    shown = {"id": last.id, "title": last.title}
    assert seen == [
        None,
        None,
        {**shown, "offerBack": False},
        {**shown, "offerBack": True},
        {**shown, "offerBack": False},
        None,
        None,
    ]
    assert loading == [None]


# --- #160: free play's input, double submits and refusals ---
# page.evaluate script: what a free-play submit does with the form's fields, given the panel's
# run (a status and its events, started at wall time 10 000) at wall time `now`.
FREE_PLAY_SUBMIT = """async ({query, status, events, now}) => {
  const reducer = await import("/static/trace-reducer.mjs");
  const run = {...reducer.idleRun(), status, startedAt: status === "idle" ? null : 10000,
               trace: reducer.foldTrace(events)};
  return reducer.freePlaySubmit({query, service_filter: " ", force_refresh: false}, run, now);
}"""
# page.evaluate script: the free-play view of a run started at wall time 10 000, at each `now`.
FREE_PLAY_AT = """async ({status, events, nows}) => {
  const reducer = await import("/static/trace-reducer.mjs");
  const run = {...reducer.idleRun(), status, startedAt: 10000, trace: reducer.foldTrace(events)};
  return nows.map((now) => {
    const view = reducer.freePlayView(run, now);
    return {line: view.shed?.line ?? null, canTriage: view.canTriage, refused: view.refused,
            retryAt: view.retryAt, ticks: reducer.freePlayTicks(run, now)};
  });
}"""
FREE_PLAY_STARTED = {**STARTED_EVENT, "scenario_id": None}
BUDGET_SHED = [
    FREE_PLAY_STARTED,
    {
        "kind": "run_slot",
        "seq": 1,
        "t_ms": 2,
        "acquired": False,
        "waited_ms": 0,
        "capacity": 4,
        "reason": "budget",
        "retry_after_s": 12,
    },
    {"kind": "trace_finished", "seq": 2, "t_ms": 3, "outcome": "shed", "response": None},
]
BUDGET_HEAD = "The free-play budget is spent for this minute."
TOO_SHORT = "Describe the incident in at least 3 characters."


def _free_play(pages: GuardedPages, script: str, arg: dict[str, Any]) -> Any:
    """Evaluate a free-play reducer script in a fresh page."""
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)
    return page.evaluate(script, arg)


@pytest.mark.parametrize("query", ["", "   ", "\n\t \n", "  ab  "])
def test_a_question_too_short_after_trimming_is_refused_with_a_plain_message(
    pages: GuardedPages, query: str
):
    """Spaces don't count: fewer than 3 characters left after trimming sends nothing."""
    submit = _free_play(
        pages, FREE_PLAY_SUBMIT, {"query": query, "status": "idle", "events": [], "now": 0}
    )

    assert submit == {"error": TOO_SHORT, "request": None}


def test_a_question_is_sent_trimmed(pages: GuardedPages):
    """Three characters after trimming are enough; the request carries the trimmed question."""
    submit = _free_play(
        pages, FREE_PLAY_SUBMIT, {"query": "  5xx \n", "status": "idle", "events": [], "now": 0}
    )

    assert submit == {
        "error": None,
        "request": {"query": "5xx", "service_filter": None, "force_refresh": False},
    }


def test_no_second_run_is_sent_while_one_streams(pages: GuardedPages):
    """A second submit while the panel's run streams sends nothing (a double click)."""
    submit = _free_play(
        pages,
        FREE_PLAY_SUBMIT,
        {"query": "Checkout down", "status": "streaming", "events": [], "now": 10_005},
    )

    assert submit == {"error": None, "request": None}


@pytest.mark.parametrize(
    ("status", "events", "now"),
    [("streaming", [], 10_005), ("finished", BUDGET_SHED, 13_500)],
    ids=["streaming", "before-retry-time"],
)
def test_an_unavailable_triage_never_says_the_question_is_too_short(
    pages: GuardedPages, status: str, events: list[dict[str, Any]], now: int
):
    """While Triage is unavailable a press sends nothing and says nothing, even for a question
    too short to send: the availability check comes before the length check."""
    submit = _free_play(
        pages, FREE_PLAY_SUBMIT, {"query": " a ", "status": status, "events": events, "now": now}
    )

    assert submit == {"error": None, "request": None}


def test_a_budget_shed_counts_down_to_its_retry_time(pages: GuardedPages):
    """The retry deadline is when the shed arrived plus its retry-after; the notice counts down
    in whole seconds to it, and Triage comes back when it passes."""
    nows = [10_000, 10_002, 13_500, 22_001, 22_002]
    views = _free_play(
        pages, FREE_PLAY_AT, {"status": "finished", "events": BUDGET_SHED, "nows": nows}
    )

    assert [view["line"] for view in views] == [
        f"{BUDGET_HEAD} Try again in 12 s.",
        f"{BUDGET_HEAD} Try again in 12 s.",
        f"{BUDGET_HEAD} Try again in 9 s.",
        f"{BUDGET_HEAD} Try again in 1 s.",
        "The free-play budget was spent for that minute. Try again now.",
    ]
    assert {view["retryAt"] for view in views} == {22_002}
    assert [view["canTriage"] for view in views] == [False, False, False, False, True]
    assert [view["ticks"] for view in views] == [True, True, True, True, False]
    assert all(view["refused"] for view in views)


def test_a_capacity_shed_has_no_countdown(pages: GuardedPages):
    """A capacity shed carries no retry-after: Triage is available again at once."""
    events = [dict(event) for event in BUDGET_SHED]
    events[1] = {**events[1], "reason": "capacity", "retry_after_s": None}

    (view,) = _free_play(
        pages, FREE_PLAY_AT, {"status": "finished", "events": events, "nows": [10_005]}
    )

    assert view == {
        "line": "Every run slot is busy right now. Try again in a moment.",
        "canTriage": True,
        "refused": True,
        "retryAt": None,
        "ticks": False,
    }


@pytest.mark.parametrize(("status", "can_triage"), [("streaming", False), ("finished", True)])
def test_a_run_that_reached_the_graph_is_not_refused(
    pages: GuardedPages, status: str, can_triage: bool
):
    """Triage waits for a streaming run; neither it nor a finished run is a refusal."""
    (view,) = _free_play(
        pages, FREE_PLAY_AT, {"status": status, "events": [FREE_PLAY_STARTED], "nows": [10_005]}
    )

    assert (view["canTriage"], view["refused"], view["ticks"]) == (can_triage, False, False)


# --- #163: deep links ---
# page.evaluate script: the element id each URL fragment (location.hash) names.
FRAGMENT_IDS = """async (hashes) => {
  const reducer = await import("/static/trace-reducer.mjs");
  return hashes.map((hash) => reducer.fragmentId(hash));
}"""


def test_a_fragment_names_its_decoded_id_and_a_malformed_one_names_none(pages: GuardedPages):
    """A percent-encoded fragment decodes; one that doesn't decode (a broken escape), or an
    empty one, names no element rather than throwing."""
    hashes = ["#free-play", "#caf%C3%A9", "", "#", "#%E0%A4%A", "#%"]

    ids = _free_play(pages, FRAGMENT_IDS, hashes)

    assert ids == ["free-play", "café", None, None, None, None]
