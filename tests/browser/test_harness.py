"""The browser tier's harness (#117): free play never reaches Groq; pages stay on the app."""

import socket
from typing import TYPE_CHECKING

import pytest
from harness import GuardedPages, LiveServer, open_shell

from resilient_triage.llm.scripted import SIMULATION_LABEL

if TYPE_CHECKING:
    from playwright.sync_api import Browser

pytestmark = pytest.mark.browser

FREE_PLAY = """async () => {
  const response = await fetch("/triage", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({query: "GitHub Actions workflows failing with 503s"}),
  });
  return {status: response.status, body: await response.json()};
}"""


def test_free_play_from_the_page_is_answered_by_the_scripted_model(pages: GuardedPages):
    """A triage request from the page runs the graph on the test's ScriptedAdapter."""
    page, watch = pages.open()
    open_shell(page, wait_for_requests=False)

    answer = page.evaluate(FREE_PLAY)

    assert answer["status"] == 200
    assert pages.server.llm.calls, "the request never reached the scripted model"
    assert pages.server.runtime.llm.label == SIMULATION_LABEL
    assert watch.failed == []


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_the_guard_blocks_another_loopback_port(browser: "Browser", live_server: LiveServer):
    """Only the live app's own origin gets through: another port on 127.0.0.1 is aborted."""
    # Its own GuardedPages, not the `pages` fixture, which fails any test that was blocked.
    guarded = GuardedPages(browser, live_server)
    try:
        page, watch = guarded.open()
        open_shell(page, wait_for_requests=False)
        elsewhere = f"http://127.0.0.1:{_free_loopback_port()}/"

        reached = page.evaluate("(url) => fetch(url).then(() => true, () => false)", elsewhere)

        assert reached is False
        assert watch.blocked == [elsewhere]
    finally:
        guarded.close()
