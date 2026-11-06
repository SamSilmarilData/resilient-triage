"""The page shell in Chromium (#117): loads clean, passes axe in both themes, fits 375 px."""

import pytest
from harness import PAGE_WIDTHS, GuardedPages, axe_violations, describe, open_shell

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
