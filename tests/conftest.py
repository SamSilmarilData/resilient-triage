"""Pytest configuration and shared fixtures for the resilient-triage test suite.

Every test gets fast retry waits and no network beyond loopback; tests that need app state
take the `runtime` fixture, a fresh AppRuntime per test, so there are no module-level
singletons to reset.
"""

import ipaddress
import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fakes import fake_statuspage_http

from resilient_triage.api.app import create_app
from resilient_triage.config import settings
from resilient_triage.llm import ScriptedAdapter
from resilient_triage.runtime import AppRuntime, build_runtime


@pytest.fixture(autouse=True)
def fast_retry_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compress retry wait intervals so retry tests don't sleep."""
    monkeypatch.setattr(settings, "retry_min_wait_seconds", 0.001)
    monkeypatch.setattr(settings, "retry_max_wait_seconds", 0.005)


def _is_local(host: object) -> bool:
    if host is None or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Block and fail any connection off loopback, so no test reaches real Groq or Statuspage.

    The app degrades instead of raising, so a blocked call alone could still pass; every
    attempt is recorded and fails the test at teardown. Loopback (local Redis) stays open.
    """
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def blocked(target: object) -> OSError:
        attempts.append(repr(target))
        return OSError(f"network access outside loopback is blocked in tests: {target!r}")

    def connect(self: socket.socket, address: Any) -> None:
        if isinstance(address, tuple) and not _is_local(address[0]):
            raise blocked(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if isinstance(address, tuple) and not _is_local(address[0]):
            raise blocked(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_local(host.decode() if isinstance(host, bytes) else host):
            raise blocked(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    hint = " (cold FastEmbed model cache? load it once outside pytest)"
    assert not attempts, f"test tried to reach the network: {attempts}" + (
        hint if any("huggingface" in a for a in attempts) else ""
    )


@pytest_asyncio.fixture
async def runtime() -> AsyncIterator[AppRuntime]:
    """A started runtime with a scripted LLM and a canned Statuspage, its cache emptied."""
    rt = build_runtime(
        settings, llm_adapter=ScriptedAdapter(), statuspage_http=fake_statuspage_http()
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
