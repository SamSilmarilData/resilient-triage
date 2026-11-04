"""The guided chapters in Chromium (#119): chapters and scenarios drawn from GET /scenarios.

A scenario opens inline (its setup notes, Play and Skip, its one-line summary, "Show
everything" and the report) and plays on the pinned stage. Everything the page shows is
checked against the live app's own catalogue and the Python summary of the same run.
"""

import json
from typing import TYPE_CHECKING, Any

import pytest
from harness import GuardedPages, open_shell

from resilient_triage.scenarios.catalogue import public_catalogue

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

CATALOGUE_READY = '[data-catalogue="ready"]'
# The chapters as drawn: each chapter's id, title and intro, and its scenarios' copy.
DRAWN_CHAPTERS = """() => [...document.querySelectorAll("[data-chapter]")].map((chapter) => ({
  id: chapter.dataset.chapter,
  title: chapter.querySelector(".chapter__title").textContent.trim(),
  intro: chapter.querySelector(".chapter__intro").textContent.trim(),
  scenarios: [...chapter.querySelectorAll("[data-scenario]")].map((scenario) => ({
    id: scenario.dataset.scenario,
    title: scenario.querySelector(".scenario__title").textContent.trim(),
    caption: scenario.querySelector(".scenario__caption").textContent.trim(),
  })),
}))"""


def _open_chapters(page: "Page") -> None:
    """Load the page and wait until the chapters have drawn from GET /scenarios."""
    open_shell(page)
    page.wait_for_selector(CATALOGUE_READY, state="attached")


def _copy(catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    """The copy the chapters should draw from a GET /scenarios body."""
    return [
        {
            "id": chapter["id"],
            "title": chapter["title"],
            "intro": chapter["intro"],
            "scenarios": [
                {"id": s["id"], "title": s["title"], "caption": s["caption"]}
                for s in chapter["scenarios"]
            ],
        }
        for chapter in catalogue["chapters"]
    ]


def test_chapters_draw_the_catalogue_from_get_scenarios(pages: GuardedPages):
    """Every chapter and scenario on the page is the catalogue's, in its order."""
    page, watch = pages.open()

    _open_chapters(page)

    expected = _copy(public_catalogue().model_dump(mode="json"))
    assert page.evaluate(DRAWN_CHAPTERS) == expected
    assert any(url.endswith("/scenarios") for url in watch.requests)
    assert watch.failed == []
    assert watch.page_errors == []


def test_the_copy_comes_from_the_response_not_the_page(pages: GuardedPages):
    """Change the served catalogue and the page draws the change: no copy lives in JS."""
    body = public_catalogue().model_dump(mode="json")
    body["chapters"] = body["chapters"][:1]
    body["chapters"][0]["title"] = "Renamed chapter"
    body["chapters"][0]["scenarios"][0]["caption"] = "A caption only the response knows."
    page, _ = pages.open()
    page.route(
        "**/scenarios",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)
        ),
    )

    _open_chapters(page)

    assert page.evaluate(DRAWN_CHAPTERS) == _copy(body)


def test_a_failed_catalogue_keeps_the_prose_and_offers_retry(pages: GuardedPages):
    """GET /scenarios failing leaves the static prose up and a notice with Retry (#101)."""
    page, watch = pages.open()
    page.route("**/scenarios", lambda route: route.fulfill(status=503, body="down"))

    open_shell(page)
    page.wait_for_selector('[data-catalogue="failed"]', state="attached")

    assert page.locator("#hook-title").is_visible()
    assert page.locator("#guided-title").is_visible()
    notice = page.locator('[data-catalogue="failed"] [role="alert"]')
    assert "didn't load" in notice.inner_text()
    assert [failure.split(" ")[0] for failure in watch.failed] == ["503"]

    page.unroute("**/scenarios")
    page.get_by_role("button", name="Retry").click()
    page.wait_for_selector(CATALOGUE_READY, state="attached")
    assert page.locator("[data-chapter]").count() == len(public_catalogue().chapters)
