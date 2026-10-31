"""Scenario routes (ADR-009): the public catalogue, scenario streams, and the free-play limits."""

import asyncio

import httpx
import pytest
from fakes import AppServer, BlockingAdapter, finished, observed, one_slot, parse_sse

from resilient_triage.llm import ScriptedAdapter
from resilient_triage.scenarios import Scenario
from resilient_triage.scenarios.catalogue import SCENARIOS
from resilient_triage.trace.events import TraceFinished, TraceStarted
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


QUERY = "Payments API returning 503 for card checkouts"


async def run_scenario(client: httpx.AsyncClient, scenario_id: str) -> TraceFinished:
    """Stream a scenario and return its trace_finished."""
    return finished(parse_sse((await client.post(f"/scenarios/{scenario_id}/stream")).text))


async def test_a_full_scenario_pool_does_not_shed_free_play(serve_app: AppServer) -> None:
    async with serve_app(ScriptedAdapter(), one_slot(scenario_max_concurrent_runs=1)) as (rt, ac):
        assert await rt.scenario_slots.acquire()
        try:
            shed_scenario = await run_scenario(ac, "healthy-run")
            free_play = await ac.post("/triage", json={"query": QUERY})
        finally:
            rt.scenario_slots.release()

    assert shed_scenario.outcome == "shed"
    assert free_play.status_code == 200


async def test_a_full_free_play_pool_does_not_shed_scenarios(serve_app: AppServer) -> None:
    llm = BlockingAdapter()
    async with serve_app(llm, one_slot()) as (_, ac):
        first = asyncio.create_task(ac.post("/triage", json={"query": QUERY}))
        await asyncio.wait_for(llm.started.wait(), timeout=5.0)
        shed_free_play = await ac.post("/triage", json={"query": "Checkout latency in eu-west-1"})
        scenario = await run_scenario(ac, "healthy-run")
        llm.release.set()
        assert (await first).status_code == 200

    assert shed_free_play.status_code == 503
    assert scenario.outcome == "analyzed"
