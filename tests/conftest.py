"""Pytest configuration and global fixtures for resilient-triage test suite.

Provides centralized singleton lifecycle resets, fast retry wait boundaries for testing,
and shared client fixtures.
"""

import httpx
import pytest_asyncio

from resilient_triage.api.app import app
from resilient_triage.cache.semantic_cache import semantic_cache
from resilient_triage.config import settings
from resilient_triage.graph.llm import (
    MockTriageChatModel,
    close_groq_async_client,
    groq_quota_tracker,
    set_triage_llm,
)
from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
from resilient_triage.telemetry.chaos import chaos_manager
from resilient_triage.telemetry.statuspage import (
    clear_statuspage_cache,
    close_statuspage_client,
)


@pytest_asyncio.fixture(autouse=True)
async def reset_global_test_environment(monkeypatch):
    """Uniformly reset application singletons, clear caches, and minimize retry delays for fast execution."""
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
async def client():
    """Yield an async test client configured with the FastAPI app lifespan."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as ac:
            yield ac
