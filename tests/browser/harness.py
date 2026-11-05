"""Browser-tier helpers (#117): the live app server, guarded pages, and the axe-core check.

Playwright is imported only for type checking here and inside functions that run in the
browser tier, so the regular test run, which doesn't install it, can still collect this file.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import resources
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import uvicorn
from fakes import fake_statuspage_http, parse_sse
from repo_scripts import vendored_axe

from resilient_triage.api.app import create_app
from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.config import Settings, settings
from resilient_triage.llm import ScriptedAdapter
from resilient_triage.runtime import AppRuntime, build_runtime
from resilient_triage.trace.events import TraceEvent

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Request, Route

T = TypeVar("T")

SERVER_START_TIMEOUT_S = 20.0
# How long a coroutine run on the server's loop (LiveServer.run) may take.
SERVER_LOOP_CALL_TIMEOUT_S = 20.0
SERVER_STOP_TIMEOUT_S = 20.0
SHELL_READY = 'html[data-shell="ready"]'
BLOCKING_IMPACTS = ("serious", "critical")

# page.evaluate script: stream one run with the page's own SSE reader (sse.mjs) and fold its
# events with the reducer. Argument: {scenario: id} or {query: text} (free play). Returns the
# raw events, the reducer's summary and its summary line.
STREAM_AND_FOLD = """async (run) => {
  const sse = await import("/static/sse.mjs");
  const reducer = await import("/static/trace-reducer.mjs");
  const source = run.scenario
    ? sse.scenarioSource(run.scenario)
    : sse.freePlaySource({query: run.query});
  const events = [];
  for await (const message of source(new AbortController().signal)) {
    if (message.kind === "event") events.push(message.event);
  }
  const summary = reducer.summarize(events);
  return {events, summary, line: reducer.summaryLine(summary)};
}"""

# page.evaluate script: start a run on the page's stage (app.mjs `stage`), the way a play
# button does, without waiting for it. Argument as STREAM_AND_FOLD's.
PLAY_ON_STAGE = """async (run) => {
  const {stage} = await import("/static/app.mjs");
  const sse = await import("/static/sse.mjs");
  stage.play(run.scenario
    ? sse.scenarioSource(run.scenario)
    : sse.freePlaySource({query: run.query}));
}"""
# page.evaluate script: the page's [scrollWidth, clientWidth]; wider than the viewport means
# it scrolls sideways.
PAGE_WIDTHS = "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
RUN_STATES = ("idle", "streaming", "finished", "lost", "error")
GRAPH_READY = '[data-graph="ready"]'
# The chapters region once GET /scenarios has drawn.
CATALOGUE_READY = '[data-catalogue="ready"]'

# Every (source, target) edge in graph-layout.json, the graph the page draws.
LAYOUT_EDGES = {
    (edge["source"], edge["target"])
    for edge in json.loads(
        resources.files("resilient_triage.api").joinpath("static/graph-layout.json").read_text()
    )["edges"]
}
# page.evaluate script: the graph drawn inside a root. `visits`: each graph node's visit count
# (__start__ and __end__ are the graph's terminals, not steps of the path). `taken`: the taken
# edges, each with its route (null on an unconditional edge). Argument: the root's selector.
GRAPH_PROBE = """(root) => {
  const graph = document.querySelector(root);
  return {
    visits: Object.fromEntries([...graph.querySelectorAll("[data-node]")]
      .filter((node) => !node.dataset.node.startsWith("__"))
      .map((node) => [node.dataset.node, Number(node.dataset.visits)])),
    taken: [...graph.querySelectorAll('[data-edge][data-taken="true"]')]
      .map((edge) => ({source: edge.dataset.source, target: edge.dataset.target,
                       route: edge.dataset.route ?? null})),
  };
}"""


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
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(SERVER_LOOP_CALL_TIMEOUT_S)


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
def serve_live_app(app_settings: Settings = settings) -> Iterator[LiveServer]:
    """Serve `create_app` over a fresh scripted runtime on `127.0.0.1:<free port>`."""
    adapter = ScriptedAdapter()
    built: list[AppRuntime] = []

    def runtime_factory() -> AppRuntime:
        # Called by the app's lifespan, in the server's loop, which then owns the runtime.
        rt = build_runtime(
            app_settings,
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
        thread.join(SERVER_STOP_TIMEOUT_S)
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
        return [url for url in self.requests if not same_origin(url, self.origin)]


def _describe_failure(request: Request) -> str:
    """One line for a failed request: method, URL and Playwright's failure text."""
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
        page.on("requestfailed", lambda request: watch.failed.append(_describe_failure(request)))
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

    def page_errors(self) -> list[str]:
        """Every uncaught error any page here threw."""
        return [error for watch in self.watches for error in watch.page_errors]

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


def stream_scenario(server: LiveServer, scenario_id: str) -> list[TraceEvent]:
    """Run a scenario on the live app from Python and return its parsed trace events."""
    response = httpx.post(f"{server.url}/scenarios/{scenario_id}/stream", timeout=60)
    assert response.status_code == 200, response.text
    return parse_sse(response.text)


def unique_question(text: str = "GitHub Actions workflows failing with 503s") -> str:
    """Free-play text no other run can have cached (a local Redis is shared across worktrees)."""
    return f"{text} (ticket {uuid4().hex[:8]})"


def play(page: Page, *, scenario: str | None = None, query: str | None = None) -> None:
    """Start a scenario or free-play run on the stage once the graph has drawn."""
    page.wait_for_selector(GRAPH_READY, state="attached")
    run = {"scenario": scenario} if scenario is not None else {"query": query}
    page.evaluate(PLAY_ON_STAGE, run)


def run_selector(state: str, root: str = "#stage-root") -> str:
    """The selector of a run in a state (`data-run-state`) inside a stage root."""
    assert state in RUN_STATES, state
    return f'{root} [data-run-state="{state}"]'


def wait_for_run(
    page: Page, state: str = "finished", root: str = "#stage-root", timeout_ms: float = 30_000
) -> None:
    """Wait until the run inside `root` (the stage's, by default) reaches a state; never a sleep."""
    page.wait_for_selector(run_selector(state, root), state="attached", timeout=timeout_ms)


@dataclass(frozen=True)
class GraphState:
    """A drawn graph: each node's visit count and the edges the run took."""

    visits: dict[str, int]
    taken: list[dict[str, str | None]]

    @property
    def visited(self) -> set[str]:
        """The graph nodes visited at least once."""
        return {node for node, count in self.visits.items() if count}

    @property
    def taken_pairs(self) -> set[tuple[str, str]]:
        """The taken edges as (source, target) pairs."""
        return {(str(edge["source"]), str(edge["target"])) for edge in self.taken}

    @property
    def taken_routes(self) -> set[str]:
        """The routes on the taken conditional edges."""
        return {str(edge["route"]) for edge in self.taken if edge["route"]}


def graph_state(page: Page, root: str = "#stage-root") -> GraphState:
    """The graph drawn inside `root` (the stage's, by default), as GRAPH_PROBE reads it."""
    probed = page.evaluate(GRAPH_PROBE, root)
    return GraphState(visits=probed["visits"], taken=probed["taken"])


def path_edges(path: list[str]) -> set[tuple[str, str]]:
    """The layout edges a node path walks, from __start__ to __end__."""
    walk = ["__start__", *path, "__end__"]
    return {pair for pair in zip(walk, walk[1:], strict=False) if pair in LAYOUT_EDGES}


def axe_violations(page: Page) -> list[dict[str, Any]]:
    """Inject axe-core from tests/vendor (never served by the app); return blocking violations."""
    axe = vendored_axe()
    page.add_script_tag(path=str(axe.path))
    result: dict[str, Any] = page.evaluate("axe.run(document, {resultTypes: ['violations']})")
    assert result["testEngine"]["version"] == axe.version
    return [v for v in result["violations"] if v["impact"] in BLOCKING_IMPACTS]


def describe(violations: list[dict[str, Any]]) -> str:
    """One line per axe violation: rule, impact, help text and the offending selectors."""
    return "\n".join(
        f"{v['id']} ({v['impact']}): {v['help']} at {[n['target'] for n in v['nodes']]}"
        for v in violations
    )
