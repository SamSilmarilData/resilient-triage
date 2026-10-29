import time
from typing import Any

import httpx
import pytest
from fakes import make_triage_deps

from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry, force_open_for_tests
from resilient_triage.schemas.telemetry import ChaosConfig
from resilient_triage.telemetry.chaos import ChaosManager, ClusterSentinelSource
from resilient_triage.telemetry.probe import TelemetryProbe
from resilient_triage.telemetry.statuspage import StatuspageSource

STATUSPAGE_URL = "https://statuspage.test/summary.json"
STATUSPAGE = frozenset({"statuspage"})
CHAOS = frozenset({"chaos"})


class CountingStatuspage:
    """httpx.MockTransport handler answering with a fixed status and body, counting requests."""

    def __init__(self, payload: dict[str, Any] | None = None, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.payload is None:
            return httpx.Response(self.status_code, content=b"Internal Server Error")
        return httpx.Response(self.status_code, json=self.payload)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def _statuspage(handler: CountingStatuspage, cache_ttl_seconds: float = 10.0) -> StatuspageSource:
    return StatuspageSource(
        url=STATUSPAGE_URL, client=handler.client(), cache_ttl_seconds=cache_ttl_seconds
    )


def _summary(indicator: str, page_name: str = "GitHub") -> dict[str, Any]:
    """A minimal Statuspage summary payload with the given indicator."""
    return {
        "page": {"name": page_name},
        "status": {"indicator": indicator, "description": f"{indicator} status"},
        "components": [],
        "incidents": [],
    }


async def test_statuspage_source_parsing_and_signals():
    """Verify StatuspageSource parses API payloads and produces TelemetrySignal records."""
    mock_payload = {
        "page": {"name": "GitHub", "url": "https://www.githubstatus.com"},
        "status": {"indicator": "major", "description": "Major Service Outage"},
        "components": [
            {"id": "c1", "name": "Git Operations", "status": "operational"},
            {
                "id": "c2",
                "name": "Actions",
                "status": "major_outage",
                "description": "Actions unavailable",
            },
        ],
        "incidents": [
            {
                "id": "inc1",
                "name": "Degraded Actions execution",
                "status": "investigating",
                "impact": "critical",
                "shortlink": "https://stspg.io/xyz",
            }
        ],
    }
    source = _statuspage(CountingStatuspage(mock_payload))

    signals = await source.read("github actions down", None)
    assert len(signals) == 3

    global_signal = next(s for s in signals if s.source == "statuspage:global")
    assert global_signal.status == "major"
    assert "GitHub" in (global_signal.raw_snippet or "")

    component_signal = next(s for s in signals if "Actions" in s.source)
    assert component_signal.status == "major_outage"
    assert component_signal.error_rate == 0.8

    incident_signal = next(s for s in signals if "incident" in s.source)
    assert incident_signal.status == "investigating"
    assert incident_signal.error_rate == 1.0


async def test_statuspage_read_raises_on_an_upstream_error():
    """The adapter only reads and parses: an upstream 500 raises, the probe degrades it."""
    source = _statuspage(CountingStatuspage(status_code=500))

    with pytest.raises(httpx.HTTPStatusError):
        await source.read("github", None)
    assert source.degraded_label("github", None) == "statuspage:global"


async def test_statuspage_circuit_breaker_fallback():
    """Statuspage reads through the probe degrade to one signal and fail fast once tripped."""
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("statuspage", fail_max=3)
    probe = TelemetryProbe([_statuspage(CountingStatuspage(status_code=500))], breakers=registry)

    # 3 failed reads trip the breaker; each, the tripping one included, reports the real error.
    for i in range(3):
        reading = await probe.collect(STATUSPAGE, "github", None)
        assert len(reading.signals) == 1
        assert reading.signals[0].source == "statuspage:global"
        # The tripping read leaves the breaker open, so it is already a Tripped breaker.
        assert reading.signals[0].status == ("UNAVAILABLE" if i < 2 else "CIRCUIT_OPEN")
        assert "500" in (reading.signals[0].raw_snippet or "")

    assert cb.info().is_open is True

    # 4th call should immediately return degraded signal with CIRCUIT_OPEN
    fallback = await probe.collect(STATUSPAGE, "github", None)
    assert len(fallback.signals) == 1
    assert fallback.signals[0].status == "CIRCUIT_OPEN"
    assert "tripped OPEN" in (fallback.signals[0].raw_snippet or "")


async def test_chaos_manager_and_cluster_sentinel_trip():
    """Verify ChaosManager runtime updates and the Cluster Sentinel breaker tripping."""
    mgr = ChaosManager()
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create("chaos", fail_max=3)
    probe = TelemetryProbe([ClusterSentinelSource(mgr)], breakers=registry)

    # 1. Healthy state
    mgr.update_config(ChaosConfig(active=False))
    reading = await probe.collect(CHAOS, "internal", None)
    assert len(reading.signals) == 1
    assert reading.signals[0].status == "OK"
    assert cb.state == "closed"

    # 2. Inject 503 chaos
    mgr.update_config(ChaosConfig(active=True, inject_503=True))

    # 3 consecutive failed operations trip the breaker
    for i in range(3):
        reading = await probe.collect(CHAOS, "internal", None)
        assert len(reading.signals) == 1
        if i < 2:
            assert reading.signals[0].status in {"UNAVAILABLE", "DEGRADED"}
        else:
            # The 3rd operation reaches the source and trips the breaker: it is Tripped, and
            # its signal still carries the real failure it saw.
            assert reading.signals[0].status == "CIRCUIT_OPEN"
            assert "503" in (reading.signals[0].raw_snippet or "")

    assert cb.info().is_open is True

    # 4th call fails fast with CIRCUIT_OPEN
    tripped = await probe.collect(CHAOS, "internal", None)
    assert len(tripped.signals) == 1
    assert tripped.signals[0].status == "CIRCUIT_OPEN"
    assert tripped.signals[0].latency_ms < 10.0  # Fast fail without hanging


async def test_statuspage_source_short_ttl_cache():
    """Verify StatuspageSource short-TTL cache prevents redundant network fetches."""
    handler = CountingStatuspage(_summary("none", page_name="CacheTest Status"))
    source = _statuspage(handler, cache_ttl_seconds=10.0)

    # First read: calls the upstream
    signals1 = await source.read("github", None)
    assert "CacheTest Status" in (signals1[0].raw_snippet or "")
    assert handler.calls == 1

    # Second read within 10s: serves from cache, no network call
    signals2 = await source.read("github", None)
    assert "CacheTest Status" in (signals2[0].raw_snippet or "")
    assert handler.calls == 1

    # A fresh source starts with an empty cache of its own and fetches again
    fresh = StatuspageSource(url=STATUSPAGE_URL, client=handler.client(), cache_ttl_seconds=10.0)
    signals3 = await fresh.read("github", None)
    assert "CacheTest Status" in (signals3[0].raw_snippet or "")
    assert handler.calls == 2


async def test_statuspage_source_with_cache_disabled_fetches_every_read():
    """A zero TTL turns the summary cache off."""
    handler = CountingStatuspage(_summary("none"))
    source = _statuspage(handler, cache_ttl_seconds=0.0)

    await source.read("github", None)
    await source.read("github", None)

    assert handler.calls == 2


async def test_chaos_latency_injection():
    """Verify that configuring latency_ms introduces actual artificial execution delay."""
    mgr = ChaosManager()
    sentinel = ClusterSentinelSource(mgr)

    mgr.update_config(ChaosConfig(active=True, latency_ms=80, inject_503=False, failure_rate=0.0))

    start = time.monotonic()
    resp = await sentinel.probe()
    elapsed_ms = (time.monotonic() - start) * 1000.0

    assert resp.status == "OK"
    assert resp.latency_injected_ms == 80
    assert elapsed_ms >= 70.0  # At least 70ms with scheduling margin


async def test_chaos_failure_rate_probabilistic():
    """Verify that failure_rate = 1.0 triggers 503 fault via the probabilistic path."""
    mgr = ChaosManager()
    sentinel = ClusterSentinelSource(mgr)

    # Injected 503 is false, but failure rate is 100%
    mgr.update_config(ChaosConfig(active=True, inject_503=False, failure_rate=1.0))

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await sentinel.probe()

    assert exc_info.value.response.status_code == 503


async def test_chaos_inactive_ignores_faults():
    """Verify that when active is False, faults (latency, 503, failure_rate) are not injected."""
    mgr = ChaosManager()
    sentinel = ClusterSentinelSource(mgr)

    # Active is False even with latency and 503 set
    mgr.update_config(ChaosConfig(active=False, latency_ms=500, inject_503=True, failure_rate=1.0))

    start = time.monotonic()
    resp = await sentinel.probe()
    elapsed_ms = (time.monotonic() - start) * 1000.0

    assert resp.status == "OK"
    assert resp.latency_injected_ms == 0
    assert elapsed_ms < 50.0  # Runs immediately without delay


@pytest.mark.parametrize(
    ("scope", "query", "expected"),
    [
        ("api_gateway", "", "api_gateway_probe"),
        (None, "Ingress router 502 errors", "api_gateway_probe"),
        ("postgres", "", "postgres_health_probe"),
        (None, "Database connection pool exhaustion", "postgres_health_probe"),
        ("payment_service", "", "payment_service_probe"),
        (None, "Checkout billing webhook timeout", "payment_service_probe"),
        ("auth-service", "", "auth_service_probe"),
        (None, "JWT session token validation latency", "auth_service_probe"),
        ("unknown_worker", "general alert", "cluster_health_probe"),
        (None, "", "cluster_health_probe"),
    ],
)
async def test_cluster_sentinel_names_the_sub_probe_matching_the_queried_subsystem(
    scope: str | None, query: str, expected: str
):
    """Cluster Sentinel names its sub-probe from the scope and query, on success and failure."""
    sentinel = ClusterSentinelSource(ChaosManager())

    assert sentinel.degraded_label(query, scope) == expected
    assert [s.source for s in await sentinel.read(query, scope)] == [expected]


async def test_cluster_sentinel_dynamic_attribution():
    """Verify reads use context-aware probe naming and return authentic error symptoms."""
    mgr = ChaosManager()
    registry = CircuitBreakerRegistry()
    registry.get_or_create("chaos", fail_max=5)
    probe = TelemetryProbe([ClusterSentinelSource(mgr)], breakers=registry)

    # 1. Healthy read with DB hint
    reading = await probe.collect(CHAOS, "Pool timeout", "postgres")
    assert len(reading.signals) == 1
    assert reading.signals[0].source == "postgres_health_probe"
    assert reading.signals[0].status == "OK"

    # 2. Inject 503 error - verify symptom contains NO simulator jargon
    mgr.update_config(ChaosConfig(active=True, inject_503=True))
    reading_err = await probe.collect(CHAOS, "Ingress dropping traffic", "gateway")
    assert len(reading_err.signals) == 1
    assert reading_err.signals[0].source == "api_gateway_probe"
    assert reading_err.signals[0].status == "UNAVAILABLE"
    # Ensure simulator jargon is eliminated
    assert "Injected Chaos Fault" not in (reading_err.signals[0].raw_snippet or "")
    assert "worker pool saturated" in (reading_err.signals[0].raw_snippet or "")


async def test_statuspage_sources_in_separate_probes_do_not_share_summaries():
    """Two Statuspage sources in probes with separate registries each fetch their own summary."""
    handler_a = CountingStatuspage(_summary("major"))
    handler_b = CountingStatuspage(_summary("none"))
    probe_a = TelemetryProbe(
        [_statuspage(handler_a, cache_ttl_seconds=60.0)], breakers=CircuitBreakerRegistry()
    )
    probe_b = TelemetryProbe(
        [_statuspage(handler_b, cache_ttl_seconds=60.0)], breakers=CircuitBreakerRegistry()
    )

    assert (await probe_a.collect(STATUSPAGE, "github", None)).signals[0].status == "major"
    assert (await probe_b.collect(STATUSPAGE, "github", None)).signals[0].status == "none"
    assert (await probe_a.collect(STATUSPAGE, "github", None)).signals[0].status == "major"
    assert handler_a.calls == 1
    assert handler_b.calls == 1


def test_the_probe_registers_breakers_in_the_registry_it_is_given():
    """A probe built with a registry creates both source breakers in it."""
    registry = CircuitBreakerRegistry()

    probe = TelemetryProbe(
        [
            ClusterSentinelSource(ChaosManager()),
            _statuspage(CountingStatuspage(_summary("none"))),
        ],
        breakers=registry,
    )

    assert probe.breakers is registry
    assert registry.get("chaos") is not None
    assert registry.get("statuspage") is not None
    assert set(registry.get_all_statuses()) == {"chaos", "statuspage"}


async def test_statuspage_sources_sharing_a_registry_still_get_their_own_cache():
    """Two Statuspage sources sharing a breaker registry never serve each other's summary."""
    handler_a = CountingStatuspage(_summary("major"))
    handler_b = CountingStatuspage(_summary("none"))
    registry = CircuitBreakerRegistry()
    probe_a = TelemetryProbe([_statuspage(handler_a, cache_ttl_seconds=60.0)], breakers=registry)
    probe_b = TelemetryProbe([_statuspage(handler_b, cache_ttl_seconds=60.0)], breakers=registry)

    assert (await probe_a.collect(STATUSPAGE, "github", None)).signals[0].status == "major"
    assert (await probe_b.collect(STATUSPAGE, "github", None)).signals[0].status == "none"
    assert handler_b.calls == 1


def test_probes_in_separate_registries_do_not_share_breakers():
    """Probes built on separate breaker registries each get fresh breakers, closed by default."""
    registry_a, registry_b = CircuitBreakerRegistry(), CircuitBreakerRegistry()
    for registry in (registry_a, registry_b):
        TelemetryProbe(
            [
                ClusterSentinelSource(ChaosManager()),
                _statuspage(CountingStatuspage(_summary("none"))),
            ],
            breakers=registry,
        )

    force_open_for_tests(registry_a.get_or_create("chaos"))
    force_open_for_tests(registry_a.get_or_create("statuspage"))

    assert registry_b.get_or_create("chaos").info().is_open is False
    assert registry_b.get_or_create("statuspage").info().is_open is False


def test_the_probe_requires_the_breaker_registry_health_reports_on():
    """The probe refuses to build a hidden registry /health would never see."""
    with pytest.raises(TypeError):
        TelemetryProbe([ClusterSentinelSource(ChaosManager())])  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # External only -> Statuspage
        ("GitHub Actions workflows are failing with 503", {"statuspage"}),
        # Internal only -> Cluster Sentinel
        ("Internal latency probe on auth-service", {"chaos"}),
        # Neither (or both) -> every source
        ("Service outage across entire infrastructure", {"statuspage", "chaos"}),
        ("Internal auth failing for GitHub webhooks", {"statuspage", "chaos"}),
    ],
)
def test_select_targets_routes_queries_by_each_adapters_keywords(query, expected):
    """Statuspage carries the external keywords and Cluster Sentinel the internal ones."""
    probe = make_triage_deps().telemetry
    assert probe.select_targets(query, None) == frozenset(expected)
