"""Source deadline: a slow Telemetry source read cannot stall a triage (bug-hunt finding 8).

These pin behaviour through the `collect_telemetry` node, `POST /triage` and the chaos
routes, not client internals, so they hold across the guarded telemetry probe refactor (#21).
"""

import asyncio
import time

import httpx
import pytest
from fakes import make_triage_deps
from langgraph.runtime import Runtime

from resilient_triage.config import settings
from resilient_triage.graph.nodes.collect_telemetry import collect_telemetry_node
from resilient_triage.schemas.telemetry import SIGNAL_STATUS_UNAVAILABLE, ChaosConfig
from resilient_triage.telemetry.chaos import ChaosManager

SOURCE_DEADLINE_S = 0.5
CHAOS_LATENCY_MS = 4000
# Matches only internal keywords; "pool" names the postgres sub-probe.
INTERNAL_QUERY = "internal database connection pool exhausted on payment workers"
EXTERNAL_QUERY = "Github API returning 503 errors across the public cloud"


@pytest.fixture(autouse=True)
def short_source_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the source deadline far below the injected latency."""
    monkeypatch.setattr(settings, "telemetry_source_deadline_s", SOURCE_DEADLINE_S)


def _slow_chaos() -> ChaosManager:
    chaos = ChaosManager()
    chaos.update_config(ChaosConfig(active=True, latency_ms=CHAOS_LATENCY_MS))
    return chaos


async def test_slow_cluster_sentinel_read_is_cut_off_at_the_source_deadline() -> None:
    deps = make_triage_deps(chaos=_slow_chaos())

    started = time.monotonic()
    result = await asyncio.wait_for(
        collect_telemetry_node({"query": INTERNAL_QUERY}, Runtime(context=deps)), timeout=10
    )
    elapsed_s = time.monotonic() - started

    assert elapsed_s < SOURCE_DEADLINE_S + 1.0
    assert "chaos" in result["unavailable_sources"]
    degraded = [s for s in result["telemetry_signals"] if s["status"] == SIGNAL_STATUS_UNAVAILABLE]
    assert [s["source"] for s in degraded] == ["postgres_health_probe"]
    assert degraded[0]["error_rate"] == 1.0
    chaos_breaker = deps.telemetry.breakers.get_all_statuses()["chaos"]
    assert chaos_breaker.fail_counter == 1
    assert chaos_breaker.state == "closed"


async def test_slow_statuspage_read_is_cut_off_at_the_source_deadline() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(CHAOS_LATENCY_MS / 1000)
        return httpx.Response(200, json={})

    deps = make_triage_deps(statuspage_http=httpx.AsyncClient(transport=httpx.MockTransport(hang)))

    started = time.monotonic()
    result = await asyncio.wait_for(
        collect_telemetry_node({"query": EXTERNAL_QUERY}, Runtime(context=deps)), timeout=10
    )
    elapsed_s = time.monotonic() - started

    assert elapsed_s < SOURCE_DEADLINE_S + 1.0
    assert "statuspage" in result["unavailable_sources"]
    degraded = [s for s in result["telemetry_signals"] if s["status"] == SIGNAL_STATUS_UNAVAILABLE]
    assert [s["source"] for s in degraded] == ["statuspage:global"]
    assert deps.telemetry.breakers.get_all_statuses()["statuspage"].fail_counter == 1


async def test_cancelling_a_read_from_outside_records_nothing_on_the_breaker() -> None:
    deps = make_triage_deps(chaos=_slow_chaos())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            collect_telemetry_node({"query": INTERNAL_QUERY}, Runtime(context=deps)),
            timeout=SOURCE_DEADLINE_S / 5,
        )

    assert deps.telemetry.breakers.get_all_statuses()["chaos"].fail_counter == 0


async def test_triage_with_slow_cluster_sentinel_returns_within_the_source_deadline(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/chaos/inject", json={"active": True, "latency_ms": CHAOS_LATENCY_MS})

    started = time.monotonic()
    resp = await client.post("/triage", json={"query": INTERNAL_QUERY, "force_refresh": True})
    elapsed_s = time.monotonic() - started

    assert resp.status_code == 200
    assert elapsed_s < SOURCE_DEADLINE_S + 2.0
    chaos = (await client.get("/resilience/status")).json()["circuits"]["chaos"]
    assert chaos["fail_counter"] == 1


async def test_chaos_probe_route_answers_within_the_source_deadline(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/chaos/inject", json={"active": True, "latency_ms": CHAOS_LATENCY_MS})

    started = time.monotonic()
    resp = await client.get("/chaos/probe")
    elapsed_s = time.monotonic() - started

    assert resp.status_code == 503
    assert elapsed_s < SOURCE_DEADLINE_S + 1.0


@pytest.mark.parametrize(("latency_ms", "expected"), [(30_000, 200), (30_001, 422)])
async def test_chaos_inject_bounds_latency(
    client: httpx.AsyncClient, latency_ms: int, expected: int
) -> None:
    resp = await client.post("/chaos/inject", json={"active": True, "latency_ms": latency_ms})

    assert resp.status_code == expected
