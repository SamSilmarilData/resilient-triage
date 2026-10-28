"""Tests for AppRuntime: per-runtime isolation, lifecycle, and the resilience snapshot."""

import socket
from collections.abc import AsyncIterator, Callable

import httpx
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


async def test_groq_llm_calls_through_this_runtimes_http_client_and_quota_tracker(
    make_runtime, monkeypatch
):
    requests: list[httpx.Request] = []

    def groq_api(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        return httpx.Response(200, json=body, headers={"x-ratelimit-remaining-requests": "41"})

    monkeypatch.setattr(
        "resilient_triage.runtime.new_groq_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(groq_api)),
    )
    groq_settings = _settings(groq_api_key="test-key", groq_model="qwen/qwen3.8-27b")
    runtime = make_runtime(groq_settings, llm_adapter=None)
    other = make_runtime(groq_settings, llm_adapter=None)

    reply = await runtime.deps.llm.ainvoke("triage this")

    assert reply.content == '{"ok": true}'
    assert len(requests) == 1
    snapshot = await runtime.snapshot()
    assert snapshot.llm_quota.total_calls == 1
    assert snapshot.llm_quota.remaining_requests == 41
    assert snapshot.active_model == "Groq LPU (qwen/qwen3.8-27b)"
    assert (await other.snapshot()).llm_quota.total_calls == 0
    assert other.quota is not runtime.quota
    assert other.quota.get_snapshot().remaining_requests is None


async def test_snapshot_reports_chaos_cache_entries_and_injected_model(make_runtime):
    runtime = make_runtime(llm_adapter=MockTriageChatModel())
    await runtime.start()
    before = await runtime.snapshot()

    runtime.chaos.update_config(ChaosConfig(active=True, inject_503=True))
    await runtime.cache.store(query="Payments API returning 503", report=_report())
    after = await runtime.snapshot()

    assert (before.chaos_active, before.cache_entries) == (False, 0)
    assert (after.chaos_active, after.cache_entries) == (True, 1)
    assert after.active_model == "Test Override (mock-triage-llm)"
    assert after.llm_quota.provider == "groq"


async def test_aclose_closes_owned_http_clients_and_cache_and_is_idempotent(
    make_runtime, monkeypatch
):
    opened: list[httpx.AsyncClient] = []

    def tracked_client() -> httpx.AsyncClient:
        client = httpx.AsyncClient()
        opened.append(client)
        return client

    monkeypatch.setattr("resilient_triage.runtime.new_groq_http_client", tracked_client)
    monkeypatch.setattr("resilient_triage.runtime.new_statuspage_http_client", tracked_client)
    # The configured Redis URL: when Redis is up, start() really connects and aclose() must drop it.
    runtime = make_runtime(
        _settings(redis_url=settings.redis_url, groq_api_key="test-key"), llm_adapter=None
    )
    await runtime.start()

    await runtime.aclose()
    await runtime.aclose()

    assert len(opened) == 2
    assert all(client.is_closed for client in opened)
    assert runtime.cache.is_redis_connected is False


async def test_building_two_runtimes_opens_no_connections(make_runtime, monkeypatch):
    attempts: list[object] = []

    def record_connect(self: socket.socket, address: object) -> None:
        attempts.append(address)
        raise OSError("network disabled in test")

    monkeypatch.setattr(socket.socket, "connect", record_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", record_connect)
    groq_settings = _settings(redis_url=settings.redis_url, groq_api_key="test-key")

    make_runtime(groq_settings, llm_adapter=None)
    make_runtime(groq_settings, llm_adapter=None)
    assert attempts == []

    # Sanity check: the guard does see I/O once a runtime starts.
    await make_runtime(groq_settings).start()
    assert attempts != []


class RecordingEmbedder(DeterministicEmbeddingProvider):
    """Deterministic embedder that records queries, optionally failing every call."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.queries: list[str] = []
        self.fail = fail

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        if self.fail:
            raise RuntimeError("embedding model unavailable")
        return super().embed_query(text)


async def test_start_warms_the_injected_embedder(make_runtime):
    embedder = RecordingEmbedder()
    runtime = make_runtime(embedder=embedder)

    await runtime.start()

    assert "warmup telemetry query" in embedder.queries
    assert len(embedder.queries) > 1


async def test_start_never_raises_and_still_registers_default_breakers(make_runtime):
    runtime = make_runtime(embedder=RecordingEmbedder(fail=True))

    async def broken_initialize() -> None:
        raise RuntimeError("cache init exploded")

    runtime.cache.initialize = broken_initialize  # type: ignore[method-assign]

    await runtime.start()

    assert set((await runtime.snapshot()).breakers) == {"statuspage", "chaos"}


async def test_snapshot_never_raises_when_the_redis_ping_errors(make_runtime):
    runtime = make_runtime()
    await runtime.start()

    async def broken_ping() -> bool:
        raise RuntimeError("ping exploded")

    runtime.cache.check_connection = broken_ping  # type: ignore[method-assign]

    snapshot = await runtime.snapshot()

    assert snapshot.cache_connected is False
    assert snapshot.cache_driver == "in_memory"


async def test_quota_starts_on_the_groq_model_of_the_settings_it_was_built_with(make_runtime):
    runtime = make_runtime(_settings(groq_model="runtime-specific-model"))

    snapshot = await runtime.snapshot()

    assert snapshot.llm_quota.active_model == "runtime-specific-model"


def _explode(*args: object, **kwargs: object) -> object:
    raise RuntimeError("member exploded")


async def test_start_never_raises_when_registering_default_breakers_fails(make_runtime):
    runtime = make_runtime()
    runtime.breakers.get_or_create = _explode  # type: ignore[method-assign]

    await runtime.start()

    assert (await runtime.snapshot()).breakers == {}


async def test_snapshot_never_raises_when_breaker_statuses_fail(make_runtime):
    runtime = make_runtime()
    await runtime.start()
    runtime.breakers.get_all_statuses = _explode  # type: ignore[method-assign]

    snapshot = await runtime.snapshot()

    assert snapshot.breakers == {}
    assert snapshot.cache_driver == "in_memory"


async def test_snapshot_never_raises_when_the_chaos_config_read_fails(make_runtime):
    runtime = make_runtime()
    await runtime.start()
    runtime.chaos.update_config(ChaosConfig(active=True, inject_503=True))
    runtime.chaos.get_config = _explode  # type: ignore[method-assign]

    snapshot = await runtime.snapshot()

    assert snapshot.chaos_active is False
    assert set(snapshot.breakers) == {"statuspage", "chaos"}


async def test_snapshot_never_raises_when_the_quota_snapshot_fails(make_runtime):
    runtime = make_runtime(_settings(groq_model="runtime-specific-model"))
    await runtime.start()
    runtime.quota.get_snapshot = _explode  # type: ignore[method-assign]

    snapshot = await runtime.snapshot()

    assert snapshot.llm_quota.active_model == "runtime-specific-model"
    assert snapshot.llm_quota.total_calls == 0
    assert snapshot.chaos_active is False
