"""The page shell in Chromium (#117): loads clean, passes axe in both themes, fits 375 px."""

import pytest
from harness import (
    PAGE_WIDTHS,
    SHELL_READY,
    GuardedPages,
    axe_violations,
    describe,
    open_shell,
)
from playwright.sync_api import Route

pytestmark = pytest.mark.browser

# Each way the page can end up light or dark: the system preference, or <html data-theme>
# overriding it either way. Values: (system color scheme, data-theme, expected theme).
THEMES = {
    "light-system": ("light", None, "light"),
    "dark-system": ("dark", None, "dark"),
    "dark-attribute": ("light", "dark", "dark"),
    "light-attribute": ("dark", "light", "light"),
}

# The page background's channels, read through a canvas so any CSS color syntax compares.
BACKGROUND_LUMA = """() => {
  const ctx = document.createElement("canvas").getContext("2d");
  ctx.fillStyle = getComputedStyle(document.body).backgroundColor;
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}"""


def test_shell_loads_clean(pages: GuardedPages):
    """`/` renders every region with no console errors, failed or off-origin requests."""
    page, watch = pages.open()

    open_shell(page)

    assert page.locator("[aria-busy]").count() == 0, "a region never finished rendering"
    assert watch.console_errors == []
    assert watch.failed == []
    assert watch.off_origin() == []
    assert any(url.endswith("/static/app.mjs") for url in watch.requests)


@pytest.mark.parametrize("theme", THEMES)
def test_landing_state_has_no_serious_axe_violations(pages: GuardedPages, theme: str):
    """axe-core finds nothing serious or critical on the landing state, in every theme path."""
    system, attribute, expected = THEMES[theme]
    page, _ = pages.open(color_scheme=system)
    open_shell(page)
    if attribute is not None:
        page.evaluate("(theme) => { document.documentElement.dataset.theme = theme; }", attribute)

    luma = page.evaluate(BACKGROUND_LUMA)
    assert (luma < 128) == (expected == "dark"), f"{theme}: page background luma {luma}"
    violations = axe_violations(page)
    assert violations == [], describe(violations)


@pytest.mark.parametrize(
    "options",
    [
        {"color_scheme": "light"},
        {"color_scheme": "dark"},
        {"color_scheme": "light", "reduced_motion": "reduce"},
    ],
    ids=["light", "dark", "reduced-motion"],
)
def test_no_horizontal_scroll_at_phone_width(pages: GuardedPages, options: dict[str, str]):
    """At 375 px wide the page never scrolls sideways."""
    page, _ = pages.open(viewport={"width": 375, "height": 812}, **options)

    open_shell(page)

    widths = page.evaluate(PAGE_WIDTHS)
    assert widths[0] <= widths[1], f"page is {widths[0]} px wide in a {widths[1]} px viewport"


# --- #163: deep links ---
DESKTOP = {"width": 1440, "height": 900}
# Set on <html> once the page has scrolled to the URL's fragment, after every region filled in.
DEEP_LINK_DONE = 'html[data-deep-link="done"]'
# Each deep-linkable section and the heading a visitor should land on.
DEEP_LINKS = {"free-play": "#free-play-title", "how-its-built": "#built-title"}
# How far below the viewport's top a landed heading may sit: the section's own top padding.
NEAR_TOP_PX = 120
HEADING_TOP = "(selector) => document.querySelector(selector).getBoundingClientRect().top"
# Resolves true once the page's scroll position holds still across two animation frames.
SCROLL_SETTLED = """() => new Promise((resolve) => {
  const y = window.scrollY;
  requestAnimationFrame(() => requestAnimationFrame(() => resolve(window.scrollY === y)));
})"""
HEADING_NEAR_TOP = f"""(selector) => {{
  const top = document.querySelector(selector).getBoundingClientRect().top;
  return top >= 0 && top <= {NEAR_TOP_PX};
}}"""


@pytest.mark.parametrize("section", DEEP_LINKS)
def test_a_deep_link_lands_on_its_section(pages: GuardedPages, section: str):
    """`/#free-play` puts Free play's heading near the top, though the chapters fill in late."""
    page, _ = pages.open(viewport=DESKTOP)

    open_shell(page, path=f"/#{section}")
    page.wait_for_selector(DEEP_LINK_DONE, state="attached")

    # Without reduced motion the scroll may be smooth: wait for it to settle, not a timer.
    page.wait_for_function(HEADING_NEAR_TOP, arg=DEEP_LINKS[section], timeout=5000)
    assert page.locator("[aria-busy]").count() == 0


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_deep_link_scrolls_once_and_instantly_under_reduced_motion(
    pages: GuardedPages, color_scheme: str
):
    """Under reduced motion the jump is instant, and it happens once: the page then stays put."""
    page, _ = pages.open(viewport=DESKTOP, color_scheme=color_scheme, reduced_motion="reduce")

    open_shell(page, path="/#free-play")
    page.wait_for_selector(DEEP_LINK_DONE, state="attached")

    top = page.evaluate(HEADING_TOP, "#free-play-title")
    assert 0 <= top <= NEAR_TOP_PX, f"heading at {top} px right after the deep-link scroll"

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready.then(() => true)")
    assert page.evaluate("window.scrollY") == 0, "the page scrolled to the fragment again"
    violations = axe_violations(page)
    assert violations == [], describe(violations)


@pytest.mark.parametrize("fragment", ["%E0%A4%A", "%"])
def test_a_malformed_deep_link_still_settles_without_a_page_error(
    pages: GuardedPages, fragment: str
):
    """A fragment that doesn't decode scrolls nowhere, throws nothing, and still marks the
    deep link done (the pages fixture fails the test on any page error)."""
    page, watch = pages.open(viewport=DESKTOP)

    open_shell(page, path=f"/#{fragment}")
    page.wait_for_selector(DEEP_LINK_DONE, state="attached")

    assert page.evaluate("window.scrollY") == 0
    assert watch.page_errors == []


def test_a_deep_link_leaves_a_visitor_who_scrolled_while_it_loaded_where_they_are(
    pages: GuardedPages,
):
    """With the chapters slow to load, a visitor who scrolls meanwhile isn't yanked to the
    fragment once they land: what they were looking at stays put."""
    page, _ = pages.open(viewport=DESKTOP, reduced_motion="reduce")
    held: list[Route] = []
    page.route("**/scenarios", lambda route: held.append(route))

    page.goto("/#how-its-built")
    page.wait_for_selector(SHELL_READY, state="attached")
    while not held:  # the catalogue's request reaches the route soon after the shell renders
        page.wait_for_timeout(20)
    page.wait_for_function(SCROLL_SETTLED)  # the browser's own jump to the fragment
    page.mouse.move(400, 400)
    page.mouse.wheel(0, -300)
    page.wait_for_function("window.scrollY > 0")
    page.wait_for_function(SCROLL_SETTLED)
    before = page.evaluate(HEADING_TOP, "#built-title")
    assert page.locator(DEEP_LINK_DONE).count() == 0, "the deep link waits for the chapters"

    held[0].continue_()
    page.wait_for_selector(DEEP_LINK_DONE, state="attached")
    page.wait_for_function(SCROLL_SETTLED)

    # The chapters grew above the view; scroll anchoring keeps the view where it was.
    after = page.evaluate(HEADING_TOP, "#built-title")
    assert abs(after - before) <= 2, f"heading moved from {before} px to {after} px"
