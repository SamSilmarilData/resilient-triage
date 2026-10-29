"""Run deadline end to end: a cancelled run leaves breakers and LLM cooldowns correct.

These go through a real AppRuntime (ScriptedAdapter LLM, chaos probe, breaker registry),
so any retry or wrapper on the path that swallowed the deadline's cancellation would show.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fakes import AppServer
from langchain_core.messages import BaseMessage

from resilient_triage.config import settings
from resilient_triage.llm import Completion, ProviderFailure, RateLimited, ScriptedAdapter

DEADLINE_S = 0.3
INTERNAL_QUERY = "Internal payment processing failing with 503 service unavailable"


@pytest.fixture(autouse=True)
def short_run_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the run deadline so every test here can outrun it."""
    monkeypatch.setattr(settings, "triage_run_deadline_s", DEADLINE_S)


async def test_chaos_latency_past_the_deadline_falls_back_and_leaves_the_probe_breaker_closed(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/chaos/inject", json={"active": True, "latency_ms": 5000})

    started = time.monotonic()
    resp = await client.post("/triage", json={"query": INTERNAL_QUERY, "force_refresh": True})
    elapsed_s = time.monotonic() - started

    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["degradation_status"] == "SEVERELY_DEGRADED"
    assert "Run deadline exceeded" in report["root_cause_analysis"]
    assert elapsed_s < DEADLINE_S + 1.0
    chaos = (await client.get("/resilience/status")).json()["circuits"]["chaos"]
    assert chaos["state"] == "closed"
    assert chaos["fail_counter"] == 0


class SlowFallbackAdapter(ScriptedAdapter):
    """A ScriptedAdapter whose calls to the fallback model hang while `slow` is set."""

    def __init__(self, script: list[str | ProviderFailure], fallback: str) -> None:
        super().__init__(script)
        self.fallback = fallback
        self.slow = True

    async def call(self, model: str, messages: list[BaseMessage]) -> Completion | ProviderFailure:
        result = await super().call(model, messages)
        if model == self.fallback and self.slow:
            await asyncio.sleep(10.0)
        return result


@pytest_asyncio.fixture
async def slow_fallback(
    serve_app: AppServer,
) -> AsyncIterator[tuple[httpx.AsyncClient, SlowFallbackAdapter]]:
    """A client over a runtime whose primary returns a 429 and whose fallback hangs."""
    limited = RateLimited(retry_after_s=60.0, scope="minute", detail="Rate limit reached")
    adapter = SlowFallbackAdapter([limited], fallback=settings.groq_fallback_model)
    async with serve_app(adapter) as (_, ac):
        yield ac, adapter


async def test_a_primary_429_before_the_deadline_still_cools_the_primary_down(
    slow_fallback: tuple[httpx.AsyncClient, SlowFallbackAdapter],
) -> None:
    client, adapter = slow_fallback
    primary, fallback = settings.groq_model, settings.groq_fallback_model

    first = await client.post("/triage", json={"query": INTERNAL_QUERY, "force_refresh": True})

    assert "Run deadline exceeded" in first.json()["report"]["root_cause_analysis"]
    assert adapter.calls == [primary, fallback]

    adapter.slow = False
    second = await client.post("/triage", json={"query": INTERNAL_QUERY, "force_refresh": True})

    assert second.status_code == 200
    assert "Run deadline exceeded" not in second.json()["report"]["root_cause_analysis"]
    later_calls = adapter.calls[2:]
    assert later_calls
    assert primary not in later_calls
