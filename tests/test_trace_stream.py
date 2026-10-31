"""POST /triage/stream (ADR-008): the execution trace over SSE, ending in one trace_finished.

The stream is parsed the way the browser will read it: `event:` names the kind, `data:` is the
JSON event, and `: ping` comment lines are skipped.
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fakes import AppServer
from langchain_core.messages import BaseMessage

from resilient_triage.config import settings
from resilient_triage.llm import Completion, ProviderFailure, ScriptedAdapter
from resilient_triage.trace.events import TraceEvent, TraceFinished, parse_trace_event

QUERY = "Payments API returning 503 for card checkouts"
# Timings, the report timestamp and its generated incident id differ between any two runs;
# everything else must match.
_PER_RUN_KEYS = {"duration_ms", "timings", "timestamp", "execution_time_ms", "incident_id"}


@pytest.fixture(autouse=True)
def fast_pings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ping every millisecond, so pings interleave with events and the parser must skip them."""
    monkeypatch.setattr("fastapi.routing._PING_INTERVAL", 0.001)


def parse_sse(body: str) -> list[TraceEvent]:
    """Parse an SSE body into trace events, checking each `event:` name matches its kind."""
    events: list[TraceEvent] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name: str | None = None
        data: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data.append(line.removeprefix("data: "))
        if not data:
            continue  # a ping (comment-only block) or trailing blank
        event = parse_trace_event("\n".join(data))
        assert event.kind == name
        events.append(event)
    return events


def without_per_run_values(value: Any) -> Any:
    """A JSON value with every per-run value (timings, timestamp, id) removed, recursively."""
    if isinstance(value, dict):
        return {k: without_per_run_values(v) for k, v in value.items() if k not in _PER_RUN_KEYS}
    if isinstance(value, list):
        return [without_per_run_values(v) for v in value]
    return value


async def stream(client: httpx.AsyncClient, body: dict[str, Any]) -> list[TraceEvent]:
    """POST /triage/stream and return its parsed events, checking the SSE response headers."""
    resp = await client.post("/triage/stream", json=body)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    return parse_sse(resp.text)


def finished(events: list[TraceEvent]) -> TraceFinished:
    """The stream's one trace_finished, which must be its last event."""
    assert [e.kind for e in events].count("trace_finished") == 1
    last = events[-1]
    assert isinstance(last, TraceFinished)
    return last


async def test_stream_ends_with_the_same_response_post_triage_returns(serve_app: AppServer):
    body = {"query": QUERY}
    async with serve_app(ScriptedAdapter()) as (_, ac):
        plain = (await ac.post("/triage", json=body)).json()
    async with serve_app(ScriptedAdapter()) as (_, ac):
        events = await stream(ac, body)

    assert events[0].kind == "trace_started"
    assert [e.seq for e in events] == list(range(len(events)))
    last = finished(events)
    assert last.outcome == "analyzed"
    assert last.response is not None
    streamed = json.loads(last.response.model_dump_json())
    assert without_per_run_values(streamed["report"]) == without_per_run_values(plain["report"])
    assert without_per_run_values(streamed["trace"]) == without_per_run_values(plain["trace"])
    assert streamed["trace"]["nodes"]


class BlockingAdapter(ScriptedAdapter):
    """A ScriptedAdapter whose first call waits until `release` is set; records a cancel."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def call(self, model: str, messages: list[BaseMessage]) -> Completion | ProviderFailure:
        if not self.started.is_set():
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return await super().call(model, messages)


def one_slot(**update: Any) -> Any:
    """Settings with a single run slot and a short queue timeout, plus any overrides."""
    return settings.model_copy(
        update={"triage_max_concurrent_runs": 1, "triage_queue_timeout_s": 0.05, **update}
    )


async def test_a_shed_stream_still_ends_with_trace_finished(serve_app: AppServer):
    llm = BlockingAdapter()
    async with serve_app(llm, one_slot()) as (_, ac):
        first = asyncio.create_task(ac.post("/triage", json={"query": QUERY}))
        await asyncio.wait_for(llm.started.wait(), timeout=5.0)
        events = await stream(ac, {"query": "Checkout latency spike in eu-west-1"})
        llm.release.set()
        assert (await first).status_code == 200

    last = finished(events)
    assert (last.outcome, last.response) == ("shed", None)
    slot = next(e for e in events if e.kind == "run_slot")
    assert slot.kind == "run_slot" and slot.acquired is False


async def test_a_stream_past_the_run_deadline_records_it_then_finishes(serve_app: AppServer):
    llm = BlockingAdapter()  # never released: the run outlives its deadline
    async with serve_app(llm, one_slot(triage_run_deadline_s=0.2)) as (_, ac):
        events = await stream(ac, {"query": QUERY})

    kinds = [e.kind for e in events]
    assert kinds.index("deadline_exceeded") < kinds.index("trace_finished")
    last = finished(events)
    assert last.outcome == "analyzed"
    assert last.response is not None
    assert last.response.report.degradation_status == "SEVERELY_DEGRADED"
    assert last.response.trace is not None and last.response.trace.deadline_exceeded


async def test_an_injected_exception_still_ends_the_stream_with_a_fallback(
    serve_app: AppServer,
):
    async with serve_app(ScriptedAdapter()) as (rt, ac):
        rt.cache.lookup = AsyncMock(side_effect=RuntimeError("Secret internal db failure"))
        resp = await ac.post("/triage/stream", json={"query": QUERY})

    assert resp.status_code == 200
    assert "Secret internal db failure" not in resp.text
    last = finished(parse_sse(resp.text))
    assert last.outcome == "analyzed"
    assert last.response is not None
    assert last.response.cached is False
    assert last.response.report.degradation_status == "SEVERELY_DEGRADED"
