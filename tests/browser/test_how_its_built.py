"""How it's built and the node inspector in Chromium (#121).

A graph node, selected by click or keyboard, opens a modal inspector with its purpose, file,
config fields and ADR links from `GET /graph/nodes`; the How it's built section links every
ADR and lists the nodes. Expected values are literals from the code (docstrings, Settings
fields, ADR file names) so a drift shows up here as well as in `tests/test_node_inspector.py`.
"""

from typing import TYPE_CHECKING

import pytest
from fakes import NODE_ORDER
from harness import GRAPH_READY, PAGE_WIDTHS, GuardedPages, axe_violations, describe, open_shell
from repo_scripts import REPO_ROOT

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

REPO_BLOB = "https://github.com/SamSilmarilData/resilient-triage/blob/main/"
INSPECTOR = "dialog.inspector"
NODES_READY = '[data-graph-nodes="ready"]'
# What the inspector shows: name, purpose, file link, config field names, ADR links.
INSPECTED = """() => {
  const dialog = document.querySelector("dialog.inspector");
  return {
    open: dialog.open,
    name: dialog.querySelector(".inspector__title").textContent,
    purpose: dialog.querySelector("[data-inspector-purpose]")?.textContent ?? null,
    file: dialog.querySelector("a[data-source-link]")?.href ?? null,
    fields: [...dialog.querySelectorAll("[data-config-field]")].map((li) => li.dataset.configField),
    adrs: [...dialog.querySelectorAll("a[data-adr-link]")].map((a) => a.href),
    focusInside: dialog.contains(document.activeElement),
  };
}"""
ACTIVE_NODE = "() => document.activeElement?.closest('[data-node]')?.dataset.node ?? null"


def open_page(pages: GuardedPages, **options: object) -> "Page":
    """Load the page and wait until the graph and the node list have both drawn."""
    page, _ = pages.open(**options)
    open_shell(page)
    page.wait_for_selector(GRAPH_READY, state="attached")
    page.wait_for_selector(NODES_READY, state="attached")
    return page


def test_keyboard_opens_the_inspector_and_escape_returns_focus_to_the_node(pages: GuardedPages):
    page = open_page(pages)

    # A graph node is a button named by its SVG <title>: its name, then whether it ran.
    page.get_by_role("button", name="generate_report, not visited").focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(f"{INSPECTOR}[open]")

    shown = page.evaluate(INSPECTED)
    assert shown == {
        "open": True,
        "name": "generate_report",
        "purpose": "Asks the model for a draft report as JSON.",
        "file": REPO_BLOB + "src/resilient_triage/graph/nodes/generate_report.py",
        "fields": [
            "groq_model",
            "groq_fallback_model",
            "llm_request_timeout_s",
            "llm_temperature",
            "llm_max_tokens",
            "llm_default_cooldown_s",
            "llm_max_cooldown_s",
        ],
        "adrs": [
            REPO_BLOB + "docs/decisions/0005-free-tier-groq-quota-monitoring-and-failover.md",
            REPO_BLOB + "docs/decisions/0006-project-owned-llm-gateway.md",
        ],
        "focusInside": True,
    }
    violations = axe_violations(page)
    assert violations == [], describe(violations)

    page.keyboard.press("Escape")

    page.wait_for_selector(f"{INSPECTOR}:not([open])", state="attached")
    assert page.evaluate(ACTIVE_NODE) == "generate_report"


def test_space_opens_the_inspector_too(pages: GuardedPages):
    page = open_page(pages)

    page.focus('[data-node="validate_report"]')
    page.keyboard.press(" ")

    page.wait_for_selector(f"{INSPECTOR}[open]")
    assert page.evaluate(INSPECTED)["fields"] == ["max_self_repair_retries"]


def test_click_opens_the_inspector_with_the_file_from_the_code_and_close_shuts_it(
    pages: GuardedPages,
):
    page = open_page(pages)

    page.click('[data-node="synthesize_telemetry"]')
    page.wait_for_selector(f"{INSPECTOR}[open]")

    shown = page.evaluate(INSPECTED)
    assert shown["name"] == "synthesize_telemetry"
    # The node's module is synthesize.py: the file comes from the code, not the node's name.
    assert shown["file"] == REPO_BLOB + "src/resilient_triage/graph/nodes/synthesize.py"
    assert shown["fields"] == []

    page.get_by_role("button", name="Close").click()

    page.wait_for_selector(f"{INSPECTOR}:not([open])", state="attached")
    assert page.evaluate(ACTIVE_NODE) == "synthesize_telemetry"


def test_how_its_built_links_every_adr_and_its_source_links_point_at_real_files(
    pages: GuardedPages,
):
    page = open_page(pages)

    hrefs: list[str] = page.eval_on_selector_all(
        "#how-its-built a[href^='https://github.com/']", "(links) => links.map((a) => a.href)"
    )

    assert hrefs, "How it's built links nothing in the repository"
    paths = [href.removeprefix(REPO_BLOB) for href in hrefs]
    assert all(href.startswith(REPO_BLOB) for href in hrefs), hrefs
    missing = [path for path in paths if not (REPO_ROOT / path).is_file()]
    assert missing == []
    adrs = {p.name for p in (REPO_ROOT / "docs" / "decisions").glob("[0-9][0-9][0-9][0-9]-*.md")}
    assert {path.rsplit("/", 1)[-1] for path in paths} >= adrs


def test_the_node_list_opens_the_inspector_and_returns_focus_to_its_button(pages: GuardedPages):
    page = open_page(pages)
    items = page.locator("[data-built-node]")

    assert items.evaluate_all("(lis) => lis.map((li) => li.dataset.builtNode)") == NODE_ORDER

    button = page.locator('[data-built-node="finalize_report"] button')
    button.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(f"{INSPECTOR}[open]")
    assert page.evaluate(INSPECTED)["file"] == (
        REPO_BLOB + "src/resilient_triage/graph/nodes/finalize.py"
    )
    page.keyboard.press("Escape")

    page.wait_for_selector(f"{INSPECTOR}:not([open])", state="attached")
    assert (
        page.evaluate(
            "() => document.activeElement?.closest('[data-built-node]')?.dataset.builtNode"
        )
        == "finalize_report"
    )


def test_when_graph_nodes_fail_the_prose_stays_and_retry_recovers(pages: GuardedPages):
    page, _ = pages.open()
    page.route("**/graph/nodes", lambda route: route.fulfill(status=503, body="down"))
    open_shell(page)
    page.wait_for_selector(GRAPH_READY, state="attached")

    page.wait_for_selector('[data-graph-nodes="failed"]', state="attached")
    assert page.locator("#how-its-built .built__list li").count() >= 7
    page.click('[data-node="analyze_query"]')
    page.wait_for_selector(f"{INSPECTOR}[open]")
    shown = page.evaluate(INSPECTED)
    assert (shown["name"], shown["purpose"], shown["file"]) == ("analyze_query", None, None)
    page.keyboard.press("Escape")

    page.unroute("**/graph/nodes")
    page.locator("[data-graph-nodes='failed']").get_by_role("button", name="Retry").click()

    page.wait_for_selector(NODES_READY, state="attached")
    assert page.locator("[data-built-node]").count() == len(NODE_ORDER)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_open_inspector_fits_phone_width_and_passes_axe(pages: GuardedPages, scheme: str):
    page = open_page(pages, viewport={"width": 375, "height": 812}, color_scheme=scheme)

    page.click('[data-node="collect_telemetry"]')
    page.wait_for_selector(f"{INSPECTOR}[open]")

    widths = page.evaluate(PAGE_WIDTHS)
    assert widths[0] <= widths[1], f"page is {widths[0]} px wide in a {widths[1]} px viewport"
    box = page.locator(INSPECTOR).bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 375
    violations = axe_violations(page)
    assert violations == [], describe(violations)


# --- #165: ADR links name their record ---
ADR_LABELS = "() => [...document.querySelectorAll('dialog.inspector a[data-adr-link]')]"
ADR_LABELS += ".map((a) => a.textContent)"


def _adr_titles_in_docs() -> dict[str, str]:
    """Each ADR's number and title, from its file's `# ADR-00N: Title` heading."""
    titles = {}
    for path in sorted((REPO_ROOT / "docs" / "decisions").glob("[0-9][0-9][0-9][0-9]-*.md")):
        heading = path.read_text().splitlines()[0]
        titles[path.name[:4]] = heading.split(": ", 1)[1]
    return titles


def test_the_pages_adr_titles_match_the_decision_records(pages: GuardedPages):
    page, _ = pages.open()
    open_shell(page, wait_for_requests=False)

    titles = page.evaluate("async () => (await import('/static/trace-reducer.mjs')).ADR_TITLES")

    assert titles == _adr_titles_in_docs()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_adr_links_carry_the_records_title_and_pass_axe(pages: GuardedPages, scheme: str):
    page = open_page(pages, color_scheme=scheme)

    page.click('[data-node="generate_report"]')
    page.wait_for_selector(f"{INSPECTOR}[open]")

    assert page.evaluate(ADR_LABELS) == [
        "ADR 0005: Free-Tier Groq Quota Monitoring and Multi-Model Failover",
        "ADR 0006: LLM Access Through a Project-Owned Gateway That Returns Failures as Values",
    ]
    assert page.locator("[data-inspector-purpose]").text_content() == (
        "Asks the model for a draft report as JSON."
    )
    violations = axe_violations(page)
    assert violations == [], describe(violations)


# --- #164: pinned Close and backdrop close ---
# Whether the element a selector names is wholly inside the viewport.
IN_VIEW = """(selector) => {
  const box = document.querySelector(selector).getBoundingClientRect();
  return box.top >= 0 && box.bottom <= window.innerHeight && box.left >= 0
    && box.right <= window.innerWidth;
}"""
CLOSE = f"{INSPECTOR} .inspector__head button"
LAST_ADR = f"{INSPECTOR} li:last-child > a[data-adr-link]"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_close_stays_in_view_while_a_long_node_scrolls_at_phone_width(
    pages: GuardedPages, scheme: str
):
    # A short phone: generate_report's seven config fields and two ADRs overflow it.
    page = open_page(pages, viewport={"width": 375, "height": 600}, color_scheme=scheme)
    page.click('[data-node="generate_report"]')
    page.wait_for_selector(f"{INSPECTOR}[open]")
    assert not page.evaluate(IN_VIEW, LAST_ADR), "the node fits: nothing to scroll"

    box = page.locator(f"{INSPECTOR} [data-inspector-purpose]").bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 2000)

    page.wait_for_function(f"() => ({IN_VIEW})({LAST_ADR!r})")
    assert page.evaluate(IN_VIEW, CLOSE), "Close scrolled out of the viewport"
    widths = page.evaluate(PAGE_WIDTHS)
    assert widths[0] <= widths[1], f"page is {widths[0]} px wide in a {widths[1]} px viewport"
    violations = axe_violations(page)
    assert violations == [], describe(violations)

    page.locator(CLOSE).click()
    page.wait_for_selector(f"{INSPECTOR}:not([open])", state="attached")
    assert page.evaluate(ACTIVE_NODE) == "generate_report"


@pytest.mark.parametrize(
    "viewport", [{"width": 1280, "height": 900}, {"width": 375, "height": 812}]
)
def test_a_click_on_the_backdrop_closes_the_inspector_and_returns_focus(
    pages: GuardedPages, viewport: dict[str, int]
):
    page = open_page(pages, viewport=viewport)
    page.click('[data-node="validate_report"]')
    page.wait_for_selector(f"{INSPECTOR}[open]")

    page.mouse.click(4, 4)

    page.wait_for_selector(f"{INSPECTOR}:not([open])", state="attached")
    assert page.evaluate(ACTIVE_NODE) == "validate_report"


def test_clicks_inside_the_inspector_do_not_close_it(pages: GuardedPages):
    # Tall enough for generate_report to fit unscrolled, so its edges are padding, not links.
    page = open_page(pages, viewport={"width": 1280, "height": 1000})
    page.click('[data-node="generate_report"]')
    page.wait_for_selector(f"{INSPECTOR}[open]")
    box = page.locator(INSPECTOR).bounding_box()
    assert box is not None

    is_open = f"() => document.querySelector({INSPECTOR!r}).open"
    page.locator(f"{INSPECTOR} [data-inspector-purpose]").click()
    page.locator(f"{INSPECTOR} .inspector__title").click()
    assert page.evaluate(is_open), "a click on the inspector's text closed it"
    # Just inside the panel's edges (mid-side, clear of the rounded corners): not the backdrop.
    for x, y in [
        (box["x"] + 3, box["y"] + box["height"] / 2),
        (box["x"] + box["width"] / 2, box["y"] + 3),
        (box["x"] + box["width"] - 3, box["y"] + box["height"] / 2),
        (box["x"] + box["width"] / 2, box["y"] + box["height"] - 3),
    ]:
        page.mouse.click(x, y)
        assert page.evaluate(is_open), f"a click at the panel's edge ({x}, {y}) closed it"
    # Pressed inside, released over the backdrop: a text selection, not a backdrop click.
    page.mouse.move(box["x"] + 20, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(4, 4)
    page.mouse.up()
    assert page.evaluate(is_open), "a drag out of the panel closed it"

    shown = page.evaluate(INSPECTED)
    assert (shown["open"], shown["focusInside"]) == (True, True)
