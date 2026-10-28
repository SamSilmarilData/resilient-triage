"""Pytest configuration and shared fixtures for the resilient-triage test suite.

Every test gets fast retry waits; tests that need app state take the `runtime` fixture, a fresh
AppRuntime per test, so there are no module-level singletons to reset.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fakes import fake_statuspage_http

from resilient_triage.api.app import create_app
from resilient_triage.config import settings
from resilient_triage.graph.llm import MockTriageChatModel
from resilient_triage.runtime import AppRuntime, build_runtime


@pytest.fixture(autouse=True)
def fast_retry_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compress retry wait intervals so retry tests don't sleep."""
    monkeypatch.setattr(settings, "retry_min_wait_seconds", 0.001)
    monkeypatch.setattr(settings, "retry_max_wait_seconds", 0.005)


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
