"""Tests for AppRuntime: per-runtime isolation, lifecycle, and the resilience snapshot."""

from collections.abc import AsyncIterator, Callable

import pytest_asyncio

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.config import Settings, settings
from resilient_triage.graph.llm import MockTriageChatModel
from resilient_triage.resilience.circuit_breaker import force_open_for_tests
from resilient_triage.runtime import AppRuntime, build_runtime

UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


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
