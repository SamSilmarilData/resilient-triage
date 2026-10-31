"""Scenario routes (ADR-009): the public catalogue, scenario streams, and the free-play limits."""

import asyncio

import httpx
import pytest
from fakes import AppServer, BlockingAdapter, finished, observed, one_slot, parse_sse

from resilient_triage.config import Settings, settings
from resilient_triage.llm import ScriptedAdapter
from resilient_triage.scenarios import Scenario
from resilient_triage.scenarios.catalogue import SCENARIOS
from resilient_triage.trace.events import RunSlot, TraceFinished, TraceStarted
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
    assert resp.json() == {"detail": "No scenario 'no-such-scenario'."}
    assert not resp.headers["content-type"].startswith("text/event-stream")


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


# Unlike each other, so none is a semantic cache hit for another.
QUERIES = (
    "Payments API returning 503 for card checkouts",
    "PostgreSQL primary replication lag above five minutes",
    "Kubernetes nodes in us-east-1 evicting pods for disk pressure",
)


def budget_of(runs: int) -> Settings:
    """Settings allowing `runs` free-play graph runs a minute."""
    return settings.model_copy(update={"free_play_runs_per_minute": runs})


async def test_graph_runs_over_the_budget_are_shed_with_retry_after(serve_app: AppServer) -> None:
    async with serve_app(ScriptedAdapter(), budget_of(2)) as (_, ac):
        ran = [await ac.post("/triage", json={"query": q}) for q in QUERIES[:2]]
        shed = await ac.post("/triage", json={"query": QUERIES[2]})
        metrics = (await ac.get("/metrics")).text

    assert [r.status_code for r in ran] == [200, 200]
    assert shed.status_code == 503
    assert 1 <= int(shed.headers["Retry-After"]) <= 60
    assert 'resilient_triage_requests_total{cache="saturated"} 1' in metrics


async def test_cache_hits_do_not_count_against_the_budget(serve_app: AppServer) -> None:
    async with serve_app(ScriptedAdapter(), budget_of(2)) as (_, ac):
        first = await ac.post("/triage", json={"query": QUERIES[0]})
        hits = [await ac.post("/triage", json={"query": QUERIES[0]}) for _ in range(3)]
        second = await ac.post("/triage", json={"query": QUERIES[1]})
        third = await ac.post("/triage", json={"query": QUERIES[2]})

    assert first.headers["X-Cache"] == "MISS"
    assert [h.headers["X-Cache"] for h in hits] == ["HIT"] * 3
    assert second.status_code == 200
    assert third.status_code == 503


async def test_a_force_refresh_run_counts(serve_app: AppServer) -> None:
    async with serve_app(ScriptedAdapter(), budget_of(1)) as (_, ac):
        await ac.post("/triage", json={"query": QUERIES[0]})
        refresh = await ac.post("/triage", json={"query": QUERIES[0], "force_refresh": True})

    assert refresh.status_code == 503


async def test_a_budget_shed_stream_names_its_reason_and_retry_after(serve_app: AppServer) -> None:
    async with serve_app(ScriptedAdapter(), budget_of(1)) as (_, ac):
        await ac.post("/triage", json={"query": QUERIES[0]})
        events = parse_sse((await ac.post("/triage/stream", json={"query": QUERIES[1]})).text)

    assert finished(events).outcome == "shed"
    [slot] = [e for e in events if isinstance(e, RunSlot)]
    assert (slot.acquired, slot.reason) == (False, "budget")
    assert slot.retry_after_s is not None and 1 <= slot.retry_after_s <= 60


async def test_a_run_shed_for_capacity_still_counts_and_says_why(serve_app: AppServer) -> None:
    llm = BlockingAdapter()
    async with serve_app(llm, one_slot(free_play_runs_per_minute=2)) as (_, ac):
        first = asyncio.create_task(ac.post("/triage", json={"query": QUERIES[0]}))
        await asyncio.wait_for(llm.started.wait(), timeout=5.0)
        no_slot = parse_sse((await ac.post("/triage/stream", json={"query": QUERIES[1]})).text)
        llm.release.set()
        assert (await first).status_code == 200
        over_budget = await ac.post("/triage", json={"query": QUERIES[2]})

    [slot] = [e for e in no_slot if isinstance(e, RunSlot)]
    assert (slot.acquired, slot.reason, slot.retry_after_s) == (False, "capacity", None)
    assert over_budget.status_code == 503
    assert "Retry-After" in over_budget.headers


async def test_scenario_runs_do_not_count_against_the_budget(serve_app: AppServer) -> None:
    async with serve_app(ScriptedAdapter(), budget_of(1)) as (_, ac):
        scenarios = [await run_scenario(ac, "healthy-run") for _ in range(2)]
        free_play = await ac.post("/triage", json={"query": QUERIES[0]})

    assert [s.outcome for s in scenarios] == ["analyzed", "analyzed"]
    assert free_play.status_code == 200
