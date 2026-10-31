"""POST /triage/stream (ADR-008): the execution trace over SSE, ending in one trace_finished.

The stream is parsed the way the browser will read it: `event:` names the kind, `data:` is the
JSON event, and `: ping` comment lines are skipped.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fakes import AppServer
from langchain_core.messages import BaseMessage

from resilient_triage.api.app import create_app
from resilient_triage.config import Settings, settings
from resilient_triage.llm import Completion, ProviderFailure, ScriptedAdapter
from resilient_triage.runtime import AppRuntime
from resilient_triage.trace.events import TraceEvent, TraceFinished, parse_trace_event

QUERY = "Payments API returning 503 for card checkouts"
# Timings, the report timestamp and its generated incident id differ between any two runs;
# everything else must match.
_PER_RUN_KEYS = {"duration_ms", "timings", "timestamp", "execution_time_ms", "incident_id"}


@pytest.fixture
def fast_pings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ping every millisecond, so pings interleave with events and the parser must skip them."""
    # Not autouse: FastAPI's keepalive task raises BrokenResourceError out of the app when a
    # client disconnects while it holds an unsent ping, which 1 ms pings make near-certain.
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


@pytest.mark.usefixtures("fast_pings")
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


Message = dict[str, Any]
ASGIApp = Callable[..., Awaitable[None]]


class RawStream:
    """Drives POST /triage/stream straight through the ASGI app, so the client can drop it."""

    # httpx's ASGITransport buffers the whole body, so it can neither see the stream begin
    # nor disconnect mid-stream; this driver can do both.

    def __init__(self, app: ASGIApp, body: dict[str, Any]) -> None:
        self._app = app
        self._body = json.dumps(body).encode()
        self._sent_body = False
        self.disconnected = asyncio.Event()
        self.first_chunk = asyncio.Event()
        self.messages: list[Message] = []

    async def _receive(self) -> Message:
        if not self._sent_body:
            self._sent_body = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        self.messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.first_chunk.set()

    async def drained(self) -> None:
        """Wait until the app has sent every event so far (the run is blocked, so it stops)."""
        # FastAPI's SSE keepalive task raises BrokenResourceError out of the app if the client
        # disconnects while it holds an unsent event, so the test drops an idle stream.
        await asyncio.wait_for(self.first_chunk.wait(), timeout=5.0)
        while True:
            sent = len(self.messages)
            await asyncio.sleep(0.02)
            if len(self.messages) == sent:
                return

    async def run(self) -> None:
        """Send the request and run the app until it returns."""
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/triage/stream",
            "raw_path": b"/triage/stream",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
        await self._app(scope, self._receive, self._send)

    @property
    def status(self) -> int:
        """The response status the app sent."""
        return next(m["status"] for m in self.messages if m["type"] == "http.response.start")


@asynccontextmanager
async def raw_app(
    serve_app: AppServer, llm: ScriptedAdapter, app_settings: Settings
) -> AsyncIterator[tuple[AppRuntime, httpx.AsyncClient, ASGIApp]]:
    """Serve an app over a fresh runtime; yield the runtime, its client and a second app on it."""
    async with serve_app(llm, app_settings) as (rt, ac):
        app = create_app(lambda: rt)  # same runtime, so the same gateway, run slots and cache
        app.state.runtime = rt
        yield rt, ac, app


async def abandoned_requests(ac: httpx.AsyncClient) -> int:
    """The abandoned-requests counter, read from /metrics."""
    text = (await ac.get("/metrics")).text
    line = next(ln for ln in text.splitlines() if ln.startswith("resilient_triage_abandoned"))
    return int(line.split()[-1])


async def assert_cleanly_abandoned(ac: httpx.AsyncClient, llm: BlockingAdapter) -> None:
    """The run was cancelled, its slot is free again, nothing was cached, it counted once."""
    assert llm.cancelled
    assert await abandoned_requests(ac) == 1
    status = (await ac.get("/resilience/status")).json()
    assert status["cache_entries_count"] == 0
    metrics = (await ac.get("/metrics")).text
    for label in ("hit", "miss", "saturated"):  # abandoned is not a triage outcome
        assert f'resilient_triage_requests_total{{cache="{label}"}} 0' in metrics
    # One slot in all: a new run gets it at once rather than being shed.
    llm.release.set()
    resp = await ac.post("/triage", json={"query": "Checkout latency spike in eu-west-1"})
    assert resp.status_code == 200
    assert resp.headers["x-cache"] == "MISS"


async def test_a_client_disconnect_cancels_the_run_and_releases_its_slot(serve_app: AppServer):
    llm = BlockingAdapter()
    async with raw_app(serve_app, llm, one_slot()) as (_, ac, app):
        client = RawStream(app, {"query": QUERY})
        request = asyncio.create_task(client.run())
        await asyncio.wait_for(llm.started.wait(), timeout=5.0)
        await client.drained()

        client.disconnected.set()
        await asyncio.wait_for(request, timeout=5.0)

        assert client.status == 200
        await assert_cleanly_abandoned(ac, llm)


async def test_an_explicitly_cancelled_stream_leaks_no_slot_or_task(serve_app: AppServer):
    llm = BlockingAdapter()
    async with raw_app(serve_app, llm, one_slot()) as (_, ac, app):
        client = RawStream(app, {"query": QUERY})
        request = asyncio.create_task(client.run())
        await asyncio.wait_for(llm.started.wait(), timeout=5.0)
        await client.drained()

        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        await assert_cleanly_abandoned(ac, llm)
