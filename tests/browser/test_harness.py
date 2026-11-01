"""The browser tier's harness (#117): free play on the live app never reaches Groq."""

import pytest
from harness import GuardedPages, open_shell

from resilient_triage.llm.scripted import SIMULATION_LABEL

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
