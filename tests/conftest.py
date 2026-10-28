"""Pytest configuration and global fixtures for resilient-triage test suite.

Provides centralized singleton lifecycle resets, fast retry wait boundaries for testing,
and shared client fixtures.
"""

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from fakes import fake_statuspage_http

from resilient_triage.api.app import create_app
from resilient_triage.cache.semantic_cache import semantic_cache
from resilient_triage.config import settings
from resilient_triage.graph.llm import (
    MockTriageChatModel,
    close_groq_async_client,
    groq_quota_tracker,
    set_triage_llm,
)
from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
from resilient_triage.runtime import AppRuntime, build_runtime
from resilient_triage.telemetry.chaos import chaos_manager
from resilient_triage.telemetry.statuspage import (
    clear_statuspage_cache,
    close_statuspage_client,
)


@pytest_asyncio.fixture(autouse=True)
async def reset_global_test_environment(monkeypatch):
    """Reset app singletons, clear caches, and minimise retry delays so tests run fast."""
    # Compress retry wait intervals in tests to eliminate idle sleeps while testing retry logic
    monkeypatch.setattr(settings, "retry_min_wait_seconds", 0.001)
    monkeypatch.setattr(settings, "retry_max_wait_seconds", 0.005)

    # 1. Reset state before test execution
    circuit_breaker_registry.reset_all()
    chaos_manager.reset()
    groq_quota_tracker.reset_counters()
    clear_statuspage_cache()
    await semantic_cache.clear()
    set_triage_llm(MockTriageChatModel())

    yield

    # 2. Reset and release connections after test execution
    circuit_breaker_registry.reset_all()
    chaos_manager.reset()
    groq_quota_tracker.reset_counters()
    clear_statuspage_cache()
    await semantic_cache.clear()
    set_triage_llm(None)
    await close_statuspage_client()
    await close_groq_async_client()
    await semantic_cache.close()


@pytest_asyncio.fixture
async def runtime() -> AsyncIterator[AppRuntime]:
    """A started runtime with the mock LLM and a canned Statuspage, its cache emptied."""
    rt = build_runtime(
        settings, llm_adapter=MockTriageChatModel(), statuspage_http=fake_statuspage_http()
    )
    await rt.start()
    # The cache may sit on a live Redis shared across runtimes: start every test empty.
    await rt.cache.clear()
    yield rt
    await rt.aclose()


@pytest_asyncio.fixture
async def client(runtime: AppRuntime) -> AsyncIterator[httpx.AsyncClient]:
    """An async test client for an app that owns the `runtime` fixture, lifespan included."""
    app = create_app(lambda: runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as ac:
            yield ac
