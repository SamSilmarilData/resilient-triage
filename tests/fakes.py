"""Hand-built fakes for TriageDeps, so graph nodes run without the app or the network."""

from typing import Any

import httpx

from resilient_triage.cache.semantic_cache import CacheMatch
from resilient_triage.config import settings
from resilient_triage.llm import ModelAdapter, ScriptedAdapter, build_llm_gateway
from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry
from resilient_triage.runtime import TelemetrySources, TriageDeps
from resilient_triage.telemetry.chaos import ChaosClient, ChaosManager
from resilient_triage.telemetry.statuspage import StatuspageClient

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
    statuspage = StatuspageClient(
        url=FAKE_STATUSPAGE_URL,
        client=statuspage_http if statuspage_http is not None else fake_statuspage_http(),
        breaker_registry=breakers,
    )
    return TriageDeps(
        llm=build_llm_gateway(settings, llm_adapter or ScriptedAdapter()),
        telemetry=TelemetrySources(
            statuspage=statuspage,
            chaos=ChaosClient(manager=chaos or ChaosManager(), breaker_registry=breakers),
            breakers=breakers,
        ),
        history=fake_history,
    )
