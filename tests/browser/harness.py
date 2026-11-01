"""Browser-tier helpers (#117): the live app server, guarded pages, and the axe-core check.

Playwright is imported only for type checking here and inside functions that run in the
browser tier, so the regular test run, which doesn't install it, can still collect this file.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

import uvicorn
from fakes import fake_statuspage_http

from resilient_triage.api.app import create_app
from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.config import settings
from resilient_triage.llm import ScriptedAdapter
from resilient_triage.runtime import AppRuntime, build_runtime

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Request, Route

T = TypeVar("T")

AXE_PATH = Path(__file__).resolve().parents[1] / "vendor" / "axe-core@4.14.0" / "axe.min.js"
SERVER_START_TIMEOUT_S = 20.0
SHELL_READY = 'html[data-shell="ready"]'
BLOCKING_IMPACTS = ("serious", "critical")


class HarnessError(RuntimeError):
    """The live app server could not start."""


def same_origin(url: str, origin: str) -> bool:
    """Whether a URL has exactly this origin: scheme, host and port."""
    ours, theirs = urlsplit(origin), urlsplit(url)
    return (ours.scheme, ours.netloc) == (theirs.scheme, theirs.netloc)


@dataclass
class LiveServer:
    """An app served by uvicorn on a background thread; `url` is its loopback origin."""

    url: str
    runtime: AppRuntime
    llm: ScriptedAdapter
    loop: asyncio.AbstractEventLoop

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on the server's loop, where the runtime lives; return its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(SERVER_START_TIMEOUT_S)


def _wait_started(server: uvicorn.Server, thread: threading.Thread) -> None:
    """Block until uvicorn is listening; raise if it exits or takes too long."""
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while not server.started:
        if not thread.is_alive():
            raise HarnessError("the live app server exited during startup")
        if time.monotonic() > deadline:
            raise HarnessError(f"the live app server didn't start in {SERVER_START_TIMEOUT_S}s")
        time.sleep(0.01)  # polls a condition under a deadline; never a fixed wait


@contextmanager
def serve_live_app() -> Iterator[LiveServer]:
    """Serve `create_app` over a fresh scripted runtime on `127.0.0.1:<free port>`."""
    adapter = ScriptedAdapter()
    built: list[AppRuntime] = []

    def runtime_factory() -> AppRuntime:
        # Called by the app's lifespan, in the server's loop, which then owns the runtime.
        rt = build_runtime(
            settings,
            llm_adapter=adapter,
            embedder=DeterministicEmbeddingProvider(),
            statuspage_http=fake_statuspage_http(),
        )
        built.append(rt)
        return rt

    app = create_app(runtime_factory)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=loop.run_until_complete, args=(server.serve(),), name="live-app", daemon=True
    )
    thread.start()
    try:
        _wait_started(server, thread)
        port = server.servers[0].sockets[0].getsockname()[1]
        live = LiveServer(f"http://127.0.0.1:{port}", built[0], adapter, loop)
        # The cache may sit on a live Redis shared across runs: start every test empty.
        live.run(live.runtime.cache.clear())
        yield live
    finally:
        server.should_exit = True
        thread.join(SERVER_START_TIMEOUT_S)
        loop.close()


@dataclass
class PageWatch:
    """What a guarded page did: requests made, blocked or failed (or 4xx/5xx), errors logged."""

    origin: str
    requests: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)

    def off_origin(self) -> list[str]:
        """Requests to anywhere but the live app's own origin."""
        return [url for url in self.requests if not url.startswith(self.origin + "/")]


def _failure(request: Request) -> str:
    return f"{request.method} {request.url}: {request.failure}"


@dataclass
class GuardedPages:
    """Opens Chromium pages on the live app, each in its own context behind an origin guard."""

    browser: Browser
    server: LiveServer
    contexts: list[BrowserContext] = field(default_factory=list)
    watches: list[PageWatch] = field(default_factory=list)

    def open(self, **context_options: Any) -> tuple[Page, PageWatch]:
        """Open a page in a new context (Playwright `new_context` options) and watch it."""
        context = self.browser.new_context(base_url=self.server.url, **context_options)
        self.contexts.append(context)
        watch = PageWatch(origin=self.server.url)
        self.watches.append(watch)

        def guard(route: Route) -> None:
            url = route.request.url
            if same_origin(url, self.server.url):
                route.continue_()
            else:
                watch.blocked.append(url)
                route.abort("blockedbyclient")

        context.route("**/*", guard)
        page = context.new_page()
        page.on("request", lambda request: watch.requests.append(request.url))
        page.on("requestfailed", lambda request: watch.failed.append(_failure(request)))
        page.on(
            "response",
            lambda response: (
                watch.failed.append(f"{response.status} {response.url}")
                if response.status >= 400
                else None
            ),
        )
        page.on(
            "console",
            lambda msg: watch.console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda error: watch.page_errors.append(str(error)))
        return page, watch

    def blocked(self) -> list[str]:
        """Every request any page here made off the live app's origin (each was aborted)."""
        return [url for watch in self.watches for url in watch.blocked]

    def close(self) -> None:
        """Close every context opened here."""
        for context in self.contexts:
            context.close()


def open_shell(page: Page, wait_for_requests: bool = True) -> None:
    """Load `/` and wait until every region of the page shell has rendered."""
    page.goto("/")
    page.wait_for_selector(SHELL_READY, state="attached")
    if wait_for_requests:
        # Fonts and other subresources settle after the shell renders: wait on them, not a timer.
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready.then(() => true)")


def axe_violations(page: Page) -> list[dict[str, Any]]:
    """Inject axe-core from tests/vendor (never served by the app); return blocking violations."""
    page.add_script_tag(path=str(AXE_PATH))
    result: dict[str, Any] = page.evaluate("axe.run(document, {resultTypes: ['violations']})")
    assert result["testEngine"]["version"] == AXE_PATH.parent.name.split("@")[1]
    return [v for v in result["violations"] if v["impact"] in BLOCKING_IMPACTS]


def describe(violations: list[dict[str, Any]]) -> str:
    """One line per axe violation: rule, impact, help text and the offending selectors."""
    return "\n".join(
        f"{v['id']} ({v['impact']}): {v['help']} at {[n['target'] for n in v['nodes']]}"
        for v in violations
    )
