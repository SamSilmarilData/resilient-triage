"""Tests for AppRuntime: per-runtime isolation, lifecycle, and the resilience snapshot."""

from collections.abc import AsyncIterator, Callable

import pytest_asyncio

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.config import Settings, settings
from resilient_triage.graph.llm import MockTriageChatModel
from resilient_triage.resilience.circuit_breaker import force_open_for_tests
from resilient_triage.runtime import AppRuntime, build_runtime
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport, SeverityLevel
from resilient_triage.schemas.telemetry import SIGNAL_STATUS_UNAVAILABLE, ChaosConfig

UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


def _report(summary: str = "Payments API returning 503") -> IncidentTriageReport:
    return IncidentTriageReport(
        summary=summary,
        severity=SeverityLevel.HIGH,
        root_cause_analysis="Upstream payment processor outage",
        degradation_status=DegradationStatus.HEALTHY,
        circuit_breakers_tripped=[],
        confidence_score=0.9,
    )


def _settings(**overrides: object) -> Settings:
    return settings.model_copy(update={"redis_url": UNREACHABLE_REDIS, **overrides})


@pytest_asyncio.fixture
async def make_runtime() -> AsyncIterator[Callable[..., AppRuntime]]:
    """Build runtimes on demand (offline embedder, mock LLM) and close them all afterwards."""
    built: list[AppRuntime] = []

    def _make(app_settings: Settings | None = None, **kwargs: object) -> AppRuntime:
        kwargs.setdefault("llm_adapter", MockTriageChatModel())
        kwargs.setdefault("embedder", DeterministicEmbeddingProvider())
        runtime = build_runtime(app_settings or _settings(), **kwargs)  # type: ignore[arg-type]
        built.append(runtime)
        return runtime

    yield _make
    for runtime in built:
        await runtime.aclose()


async def test_tripping_a_breaker_in_one_runtime_leaves_the_other_closed(make_runtime):
    first, second = make_runtime(), make_runtime()
    await first.start()
    await second.start()

    force_open_for_tests(first.breakers.get_or_create("statuspage"))

    tripped = await first.snapshot()
    untouched = await second.snapshot()
    assert tripped.breakers["statuspage"].is_open
    assert untouched.breakers["statuspage"].state == "closed"
    assert not untouched.breakers["statuspage"].is_open


async def test_redis_unreachable_start_and_snapshot_report_memory_cache(make_runtime):
    runtime = make_runtime(_settings(redis_url=UNREACHABLE_REDIS))

    await runtime.start()
    snapshot = await runtime.snapshot()

    assert snapshot.cache_driver == "in_memory"
    assert snapshot.cache_connected is False


async def test_deps_carry_the_injected_llm(make_runtime):
    adapter = MockTriageChatModel()
    runtime = make_runtime(llm_adapter=adapter)

    assert runtime.deps.llm is adapter


async def test_deps_history_reads_what_the_cache_writer_stored(make_runtime):
    runtime = make_runtime()
    await runtime.start()
    query = "Payments API returning 503 for card checkouts"

    await runtime.deps.cache_writer.store(query=query, report=_report(), service_filter="billing")
    match = await runtime.deps.history.lookup(query=query, service_filter="billing", threshold=0.7)

    assert match is not None
    assert match.report.summary == "Payments API returning 503"


async def test_deps_telemetry_chaos_probe_follows_this_runtimes_chaos_config(make_runtime):
    chaotic, calm = make_runtime(), make_runtime()
    chaotic.chaos.update_config(ChaosConfig(active=True, inject_503=True))

    chaotic_signals = await chaotic.deps.telemetry.chaos.fetch_signals()
    calm_signals = await calm.deps.telemetry.chaos.fetch_signals()

    assert chaotic_signals[0].status == SIGNAL_STATUS_UNAVAILABLE
    assert calm_signals[0].status == "OK"
    assert (await chaotic.snapshot()).breakers["chaos"].fail_counter == 1
    assert (await calm.snapshot()).breakers["chaos"].fail_counter == 0


async def test_deps_telemetry_statuspage_fails_fast_on_this_runtimes_open_breaker(make_runtime):
    runtime = make_runtime()
    force_open_for_tests(runtime.breakers.get_or_create("statuspage"))

    signals = await runtime.deps.telemetry.statuspage.fetch_signals()

    assert [s.status for s in signals] == ["CIRCUIT_OPEN"]
