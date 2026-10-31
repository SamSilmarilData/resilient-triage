"""Scenario routes (ADR-009): the public catalogue, scenario streams, and the free-play limits."""

import httpx
import pytest
from fakes import finished, observed, parse_sse

from resilient_triage.scenarios import Scenario
from resilient_triage.scenarios.catalogue import SCENARIOS
from resilient_triage.trace.events import TraceStarted
from resilient_triage.trace.summary import summarize

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


async def test_get_scenarios_lists_the_public_view_by_chapter_in_display_order(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/scenarios")

    assert resp.status_code == 200
    chapters = resp.json()["chapters"]
    assert [c["id"] for c in chapters] == list(DISPLAY_ORDER)
    assert {c["id"]: [s["id"] for s in c["scenarios"]] for c in chapters} == DISPLAY_ORDER
    assert chapters[0]["title"] == "Fast paths"
    healthy = chapters[0]["scenarios"][0]
    assert set(healthy) == {"id", "chapter", "title", "caption", "setup_notes"}
    assert healthy["chapter"] == "fast-paths"


async def _shared_state(client: httpx.AsyncClient) -> tuple[str, object, object]:
    """What a scenario run must leave untouched: /metrics, /llm/quota, /resilience/status."""
    metrics = (await client.get("/metrics")).text
    quota = (await client.get("/llm/quota")).json()
    status = (await client.get("/resilience/status")).json()
    return metrics, quota, status


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_a_scenario_streams_its_expected_outcome_and_leaves_shared_state_alone(
    client: httpx.AsyncClient, scenario: Scenario
) -> None:
    before = await _shared_state(client)

    resp = await client.post(f"/scenarios/{scenario.id}/stream")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    last = finished(events)
    started = [e for e in events if isinstance(e, TraceStarted)]
    assert [e.scenario_id for e in started] == [scenario.id]
    assert observed(events) == scenario.expected_outcome
    if last.response is not None:
        assert last.response.trace == summarize(events[:-1])
    assert await _shared_state(client) == before


async def test_an_unknown_scenario_is_a_404(client: httpx.AsyncClient) -> None:
    resp = await client.post("/scenarios/no-such-scenario/stream")

    assert resp.status_code == 404
