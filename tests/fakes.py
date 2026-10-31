"""Hand-built fakes for TriageDeps, so graph nodes run without the app or the network."""

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
from langchain_core.messages import BaseMessage

from resilient_triage.cache.semantic_cache import CacheMatch
from resilient_triage.config import settings
from resilient_triage.llm import (
    Completion,
    ModelAdapter,
    ProviderFailure,
    ScriptedAdapter,
    build_llm_gateway,
)
from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry
from resilient_triage.runtime import AppRuntime, TriageDeps
from resilient_triage.scenarios import ExpectedOutcome
from resilient_triage.telemetry.chaos import ChaosManager, ClusterSentinelSource
from resilient_triage.telemetry.probe import TelemetryProbe
from resilient_triage.telemetry.statuspage import StatuspageSource
from resilient_triage.trace.events import (
    CacheStore,
    RouteDecided,
    TraceEvent,
    TraceFinished,
    parse_trace_event,
)
from resilient_triage.trace.summary import summarize

AppServer = Callable[..., AbstractAsyncContextManager[tuple[AppRuntime, httpx.AsyncClient]]]
"""The conftest `serve_app` factory: (llm_adapter, app_settings=settings) -> (runtime, client)."""

FAKE_STATUSPAGE_URL = "https://statuspage.test/api/v2/summary.json"

OPERATIONAL_SUMMARY: dict[str, Any] = {
    "page": {"name": "GitHub", "url": "https://www.githubstatus.com"},
    "status": {"indicator": "none", "description": "All Systems Operational"},
    "components": [],
    "incidents": [],
}


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeHistory:
    """In-memory CacheReader: records lookups, returns a preset lookup match."""

    def __init__(self, match: CacheMatch | None = None) -> None:
        self.match = match
        self.lookups: list[tuple[str, str | None, float]] = []

    async def lookup(
        self, query: str, service_filter: str | None, threshold: float
    ) -> CacheMatch | None:
        self.lookups.append((query, service_filter, threshold))
        return self.match


def fake_statuspage_http(payload: dict[str, Any] | None = None) -> httpx.AsyncClient:
    """An httpx client that answers every request with a canned Statuspage summary."""
    body = payload if payload is not None else OPERATIONAL_SUMMARY
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )


def make_triage_deps(
    *,
    llm_adapter: ModelAdapter | None = None,
    history: FakeHistory | None = None,
    statuspage_http: httpx.AsyncClient | None = None,
    breakers: CircuitBreakerRegistry | None = None,
    chaos: ChaosManager | None = None,
) -> TriageDeps:
    """Build TriageDeps from fresh fakes: scripted LLM, canned Statuspage, in-memory history."""
    breakers = breakers if breakers is not None else CircuitBreakerRegistry()
    fake_history = history if history is not None else FakeHistory()
    statuspage = StatuspageSource(
        url=FAKE_STATUSPAGE_URL,
        client=statuspage_http if statuspage_http is not None else fake_statuspage_http(),
    )
    sentinel = ClusterSentinelSource(chaos or ChaosManager())
    return TriageDeps(
        llm=build_llm_gateway(settings, llm_adapter or ScriptedAdapter()),
        telemetry=TelemetryProbe([statuspage, sentinel], breakers=breakers),
        history=fake_history,
    )


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


def finished(events: list[TraceEvent]) -> TraceFinished:
    """The stream's one trace_finished, which must be its last event."""
    assert [e.kind for e in events].count("trace_finished") == 1
    last = events[-1]
    assert isinstance(last, TraceFinished)
    return last


def observed(events: Sequence[TraceEvent]) -> ExpectedOutcome:
    """What a scenario run's trace showed, in the shape its expected outcome is declared."""
    summary = summarize(events)
    last = events[-1]
    assert isinstance(last, TraceFinished)
    report = last.response.report if last.response is not None else None
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
