"""Scenario catalogue: the 13 scenarios, in display order, each run for real against its outcome."""

import re

import pytest

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.config import settings
from resilient_triage.graph.builder import route_labels, triage_graph
from resilient_triage.llm import ScriptedReply
from resilient_triage.scenarios import ExpectedOutcome, Scenario, ScenarioFactory
from resilient_triage.scenarios.catalogue import CHAPTERS, SCENARIOS, get_scenario
from resilient_triage.trace.events import CacheStore, RouteDecided, TraceEvent
from resilient_triage.trace.recorder import ListTrace
from resilient_triage.trace.summary import summarize
from resilient_triage.triage.gateway import Shed, TriageOutcome
from resilient_triage.triage.slots import RunSlotPool

DISPLAY_ORDER = {
    "fast-paths": ["healthy-run", "cache-hit", "model-failover"],
    "model-misbehaves": [
        "self-repair",
        "refine",
        "refine-then-provider-failure",
        "self-repair-exhausted",
        "reconciliation",
    ],
    "dependencies-fail": ["unavailable-source", "tripped-breaker", "run-deadline", "shed-request"],
    "finale": ["everything-down"],
}
KEBAB_CASE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def test_scenarios_are_listed_by_chapter_in_display_order() -> None:
    assert [chapter.id for chapter in CHAPTERS] == list(DISPLAY_ORDER)
    assert [(s.chapter, s.id) for s in SCENARIOS] == [
        (chapter, scenario_id)
        for chapter, scenario_ids in DISPLAY_ORDER.items()
        for scenario_id in scenario_ids
    ]


def test_ids_are_unique_kebab_case_and_every_chapter_has_a_title() -> None:
    ids = [s.id for s in SCENARIOS] + [c.id for c in CHAPTERS]
    assert len(ids) == len(set(ids))
    assert all(KEBAB_CASE.match(i) for i in ids), ids
    assert all(chapter.title for chapter in CHAPTERS)
    assert all(scenario.title and scenario.caption for scenario in SCENARIOS)


def test_a_scenario_is_found_by_its_id() -> None:
    assert get_scenario("cache-hit") is SCENARIOS[1]
    assert get_scenario("no-such-scenario") is None


def _observed(outcome: TriageOutcome, events: tuple[TraceEvent, ...]) -> ExpectedOutcome:
    """What a scenario run showed, in the shape its expected outcome is declared."""
    summary = summarize(events)
    report = None if isinstance(outcome, Shed) else outcome.report
    stores = [e.outcome for e in events if isinstance(e, CacheStore)]
    reconciliation = summary.reconciliation
    assert summary.outcome is not None
    return ExpectedOutcome(
        outcome=summary.outcome,
        degradation=report.degradation_status if report else None,
        nodes=tuple(visit.node for visit in summary.nodes),
        routes=tuple((e.source, e.route) for e in events if isinstance(e, RouteDecided)),
        model_calls=tuple((call.role, call.outcome) for call in summary.model_calls),
        telemetry=tuple((read.source, read.outcome) for read in summary.telemetry),
        breakers_tripped=tuple(report.circuit_breakers_tripped) if report else (),
        approval_raised=tuple(reconciliation.approval_raised) if reconciliation else (),
        confidence=report.confidence_score if report else None,
        cache_store=stores[0] if stores else None,
        deadline_exceeded=summary.deadline_exceeded,
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_every_scenario_run_shows_exactly_its_expected_outcome(scenario: Scenario) -> None:
    factory = ScenarioFactory(
        settings=settings,
        slots=RunSlotPool(capacity=4, queue_timeout_s=settings.triage_queue_timeout_s),
        embedder=DeterministicEmbeddingProvider(),
    )
    trace = ListTrace()

    outcome = await factory.build(scenario).triage(trace=trace)

    assert _observed(outcome, trace.events) == scenario.expected_outcome


def test_the_catalogue_covers_every_router_branch() -> None:
    branches = {(source, key) for (source, _target), key in route_labels(triage_graph).items()}

    declared = {route for s in SCENARIOS for route in s.expected_outcome.routes}

    assert len(branches) == 9
    assert declared == branches


def test_every_scripted_reply_declares_the_prototype_delay() -> None:
    for scenario in SCENARIOS:
        for entry in scenario.script:
            assert isinstance(entry, ScriptedReply), scenario.id
            assert entry.delay_s == (0.9 if isinstance(entry.reply, str) else 0.18), scenario.id


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_the_public_view_shows_the_page_copy_and_never_the_script(scenario: Scenario) -> None:
    view = scenario.public_view()

    dumped = view.model_dump()
    assert set(dumped) == {"id", "chapter", "title", "caption", "setup_notes"}
    assert (dumped["id"], dumped["chapter"]) == (scenario.id, scenario.chapter)
    assert dumped["setup_notes"] == list(scenario.setup_notes)
    wire = view.model_dump_json()
    replies = [e.reply for e in scenario.script if isinstance(e, ScriptedReply)]
    assert not any(isinstance(r, str) and r in wire for r in replies)
    assert scenario.query not in wire


def test_scenarios_with_setup_say_so_in_their_setup_notes() -> None:
    plain = {s.id for s in SCENARIOS if not s.setup_notes}

    assert plain == {"healthy-run"}
