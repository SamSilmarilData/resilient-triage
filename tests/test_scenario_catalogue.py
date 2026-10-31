"""Scenario catalogue: the 13 scenarios, in display order, each run for real against its outcome."""

import re

from resilient_triage.scenarios.catalogue import CHAPTERS, SCENARIOS, get_scenario

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
