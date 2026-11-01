"""The page shell in Chromium (#117): loads clean, passes axe in both themes, fits 375 px."""

import pytest
from harness import GuardedPages, axe_violations, describe, open_shell

pytestmark = pytest.mark.browser

# Each way the page can end up light or dark: the system preference, or <html data-theme>
# overriding it either way.
THEMES = {
    "light-system": ("light", None),
    "dark-system": ("dark", None),
    "dark-attribute": ("light", "dark"),
    "light-attribute": ("dark", "light"),
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
    assert watch.page_errors == []
    assert watch.failed == []
    assert watch.off_origin() == []
    assert any(url.endswith("/static/app.mjs") for url in watch.requests)


@pytest.mark.parametrize("theme", THEMES)
def test_landing_state_has_no_serious_axe_violations(pages: GuardedPages, theme: str):
    """axe-core finds nothing serious or critical on the landing state, in every theme path."""
    system, attribute = THEMES[theme]
    expected = theme.split("-")[0]
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

    widths = page.evaluate(
        "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
    )
    assert widths[0] <= widths[1], f"page is {widths[0]} px wide in a {widths[1]} px viewport"
