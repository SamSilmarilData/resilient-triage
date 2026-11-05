"""Accessibility and phone width in Chromium (#122).

A keyboard-only walk through the guided flow (hook to a scenario to free play), the page at
375 px after runs and with the inspector open, and axe on every chapter's final state. The
landing state's axe check lives in test_shell.py, across all four theme paths.
"""

from typing import TYPE_CHECKING, Any

import pytest
from harness import (
    CATALOGUE_READY,
    GRAPH_READY,
    PAGE_WIDTHS,
    GuardedPages,
    axe_violations,
    describe,
    open_shell,
    unique_question,
    wait_for_run,
)

from resilient_triage.scenarios.catalogue import public_catalogue

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

PHONE = {"width": 375, "height": 812}
DESKTOP = {"width": 1280, "height": 900}
# Tab presses allowed to reach a target before the walk counts as lost.
MAX_TABS = 80
# The page's regions in reading order, as FOCUS_STATE names them: Tab must visit them in it.
READING_ORDER = ["hook", "chapters", "stage", "free-play"]

# page.evaluate script: what a sighted keyboard user sees of the focused element. `ring`: an
# outline of at least 2 px, or (an SVG graph node) its box stroked in --focus. `forward`: it
# follows the element focused at the previous call in document order. `region`: the reading-
# order region it sits in (READING_ORDER), or null. Transitions on the element and the page's
# scroll settle first. Argument: a selector the walk is heading for.
FOCUS_STATE = """async (target) => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  // A ring that fades in counts once its transition ends.
  await Promise.all(el.getAnimations({subtree: true}).map((a) => a.finished.catch(() => null)));
  // Focus scrolls the page smoothly unless motion is reduced: measure once it stops.
  const frame = () => new Promise((done) => requestAnimationFrame(done));
  for (let still = 0, y = scrollY, n = 0; still < 3 && n < 120; n++) {
    await frame();
    still = scrollY === y ? still + 1 : 0;
    y = scrollY;
  }
  const probe = document.createElement("span");
  probe.style.color = "var(--focus)";
  document.body.append(probe);
  const focus = getComputedStyle(probe).color;
  probe.remove();
  const style = getComputedStyle(el);
  const box = el.querySelector(".graph__box");
  const ring = (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2)
    || (box !== null && getComputedStyle(box).stroke === focus);
  const r = el.getBoundingClientRect();
  const inView = r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0
    && r.top < innerHeight && r.left < innerWidth;
  const previous = window.__a11yLastFocus;
  window.__a11yLastFocus = el;
  const forward = !previous || previous === el || !previous.isConnected
    || Boolean(previous.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
  const name = el.id ? `#${el.id}` : el.tagName.toLowerCase()
    + (el.dataset.node ? `[data-node=${el.dataset.node}]` : "")
    + (el.dataset.action ? `[data-action=${el.dataset.action}]` : "")
    + (el.getAttribute("href") ? `[href=${el.getAttribute("href")}]` : "");
  const regions = {hook: ".skip-link, .site-header, #hook", chapters: "#chapters-root",
    stage: ".stage", "free-play": "#free-play"};
  const region = Object.keys(regions).find((key) => el.closest(regions[key])) ?? null;
  return {
    name, ring, inView, forward, region,
    focusVisible: el.matches(":focus-visible"),
    atTarget: el.matches(target),
  };
}"""
# page.evaluate script: whether an element's top edge is on screen, so it reads from its start.
TOP_IN_VIEW = """(selector) => {
  const top = document.querySelector(selector).getBoundingClientRect().top;
  return top >= 0 && top < innerHeight;
}"""
# page.evaluate script: every element that scrolls sideways inside itself (overflow-x auto or
# scroll with content wider than its box), as "tag.class" strings.
SIDEWAYS_SCROLLERS = """() => [...document.querySelectorAll("body *")]
  .filter((el) => ["auto", "scroll"].includes(getComputedStyle(el).overflowX)
    && el.scrollWidth > el.clientWidth + 1)
  .map((el) => `${el.tagName.toLowerCase()}.${el.className}`)"""
# page.evaluate script: the open inspector's box against the viewport.
DIALOG_FIT = """() => {
  const r = document.querySelector("dialog.inspector[open]").getBoundingClientRect();
  return {left: r.left, right: r.right, viewport: document.documentElement.clientWidth};
}"""
PHONE_THEMES = {
    "light": {"color_scheme": "light"},
    "dark": {"color_scheme": "dark"},
    "reduced-motion": {"color_scheme": "light", "reduced_motion": "reduce"},
}


def _check_focus(state: dict[str, Any] | None) -> dict[str, Any]:
    """Fail unless the focused element shows a visible ring, in view, after the last one."""
    assert state is not None, "focus fell back to the page body"
    assert state["focusVisible"], f"{state['name']} is focused without :focus-visible"
    assert state["ring"], f"{state['name']} has no visible focus ring"
    assert state["inView"], f"{state['name']} is focused off screen"
    assert state["forward"], f"Tab moved focus backwards to {state['name']}"
    return state


def _tab_to(page: "Page", target: str, regions: list[str] | None = None) -> list[str]:
    """Tab to `target`, checking each stop; return the stops, adding their regions to `regions`."""
    stops: list[str] = []
    for _ in range(MAX_TABS):
        page.keyboard.press("Tab")
        state = _check_focus(page.evaluate(FOCUS_STATE, target))
        stops.append(state["name"])
        if regions is not None and state["region"] is not None:
            regions.append(state["region"])
        if state["atTarget"]:
            return stops
    raise AssertionError(f"{target} not reached in {MAX_TABS} tabs: {stops}")


def _reading_order(regions: list[str]) -> list[str]:
    """The regions Tab visited, each run of repeats folded to one."""
    return [region for i, region in enumerate(regions) if i == 0 or regions[i - 1] != region]


def _focused(page: "Page", target: str) -> None:
    """Assert `target` has focus now (after an activation moved it), with a visible ring."""
    state = _check_focus(page.evaluate(FOCUS_STATE, target))
    assert state["atTarget"], f"expected focus on {target}, found {state['name']}"


def _announcement(page: "Page", root: str) -> Any:
    """The run live region inside a stage root: exactly one polite status region."""
    regions = page.locator(f'{root} [role="status"]')
    assert regions.count() == 1, f"{root} has {regions.count()} status regions"
    return regions


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_the_guided_flow_completes_with_the_keyboard_alone(
    pages: GuardedPages, viewport: dict[str, int]
):
    """Hook to scenario to free play by keyboard: focus visible, in order, runs announced."""
    first = public_catalogue().chapters[0].scenarios[0].id
    second = public_catalogue().chapters[0].scenarios[1].id
    page, watch = pages.open(viewport=viewport)
    open_shell(page)
    page.wait_for_selector(CATALOGUE_READY, state="attached")
    page.wait_for_selector(GRAPH_READY, state="attached")
    regions: list[str] = []

    # The hook: the skip link comes first, then the section nav jumps to the scenarios.
    assert _tab_to(page, ".skip-link", regions) == ["a[href=#main]"]
    _tab_to(page, 'a[href="#guided"]', regions)
    page.keyboard.press("Enter")

    # A scenario: open it, play it, and hear the run finish.
    _tab_to(page, f"#scenario-{first}-toggle", regions)
    page.keyboard.press("Enter")
    assert page.locator(f"#scenario-{first}-toggle").get_attribute("aria-expanded") == "true"
    _tab_to(page, f'[data-scenario="{first}"] [data-action="play"]', regions)
    region = _announcement(page, "#stage-root")
    assert region.inner_text() == ""
    page.keyboard.press("Enter")
    page.wait_for_function(TOP_IN_VIEW, arg="#stage-root .graph")
    wait_for_run(page)
    line = page.locator("#stage-root [data-run-line]").inner_text()
    assert region.inner_text() == f"Run finished: {line}"
    # Focus stays on Play while the stage scrolls into view (below the chapters at phone width).
    assert page.evaluate("document.activeElement.dataset.action") == "play"

    # The next scenario, by Skip to end: its run's end state shows at once, focus stays put.
    _tab_to(page, f"#scenario-{second}-toggle", regions)
    page.keyboard.press("Enter")
    skip = f'[data-scenario="{second}"] [data-action="skip"]'
    _tab_to(page, skip, regions)
    page.keyboard.press("Enter")
    page.wait_for_selector(f'[data-scenario="{second}"] [data-report]', state="attached")
    line = page.locator("#stage-root [data-run-line]").inner_text()
    assert region.inner_text() == f"Run finished: {line}"
    _focused(page, skip)

    # Free play: the form in order, submitted from the keyboard, its run announced too.
    _tab_to(page, "#free-play-query", regions)
    page.keyboard.type(unique_question("Checkout latency spiking in eu-west"))
    assert _tab_to(page, "#free-play-service", regions) == ["#free-play-service"]
    assert _tab_to(page, '[name="force_refresh"]', regions) == ["input"]
    assert _tab_to(page, '#free-play-root [type="submit"]', regions) == ["button"]
    page.keyboard.press("Enter")
    wait_for_run(page, root="#free-play-root")
    free_line = page.locator("#free-play-root [data-run-line]").inner_text()
    assert _announcement(page, "#free-play-root").inner_text() == f"Run finished: {free_line}"
    # Tab followed reading order: the chapters before the stage they play on, at every width.
    assert _reading_order(regions) == READING_ORDER
    assert watch.failed == []


def _fits(page: "Page", when: str) -> None:
    """Fail if the page, or anything in it, scrolls sideways."""
    scroll_width, client_width = page.evaluate(PAGE_WIDTHS)
    assert scroll_width <= client_width, f"{when}: page is {scroll_width} px in {client_width} px"
    assert page.evaluate(SIDEWAYS_SCROLLERS) == [], f"{when}: an element scrolls sideways"


@pytest.mark.parametrize("theme", PHONE_THEMES)
def test_phone_width_never_scrolls_sideways_after_runs_or_with_the_inspector(
    pages: GuardedPages, theme: str
):
    """At 375 px: a played scenario, a free-play run and the open inspector all fit."""
    scenario = public_catalogue().chapters[-1].scenarios[-1].id
    page, _ = pages.open(viewport=PHONE, **PHONE_THEMES[theme])
    open_shell(page)
    page.wait_for_selector(CATALOGUE_READY, state="attached")
    page.wait_for_selector(GRAPH_READY, state="attached")
    _fits(page, "landing")

    page.locator(f"#scenario-{scenario}-toggle").click()
    page.locator(f'[data-scenario="{scenario}"] [data-action="play"]').click()
    wait_for_run(page)
    page.locator(f'[data-scenario="{scenario}"] [data-action="show-everything"]').click()
    _fits(page, "after a scenario run")

    page.get_by_label("Your incident").fill(unique_question("Payments API returning 502s"))
    page.locator("#free-play-root").get_by_role("button", name="Triage").click()
    wait_for_run(page, root="#free-play-root")
    _fits(page, "after a free-play run")

    page.locator('#stage-root [data-node="generate_report"]').click()
    page.wait_for_selector("dialog.inspector[open]")
    _fits(page, "with the inspector open")
    box = page.evaluate(DIALOG_FIT)
    assert box["left"] >= 0 and box["right"] <= box["viewport"], f"inspector overflows: {box}"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("chapter_id", [chapter.id for chapter in public_catalogue().chapters])
def test_every_chapters_final_state_has_no_serious_axe_violations(
    pages: GuardedPages, chapter_id: str, scheme: str
):
    """Play a chapter's scenarios in order, opening each in turn; its end state passes axe."""
    chapter = next(c for c in public_catalogue().chapters if c.id == chapter_id)
    ids = [scenario.id for scenario in chapter.scenarios]
    page, watch = pages.open(color_scheme=scheme)
    open_shell(page)
    page.wait_for_selector(CATALOGUE_READY, state="attached")
    page.wait_for_selector(GRAPH_READY, state="attached")

    for scenario_id in ids:
        page.locator(f"#scenario-{scenario_id}-toggle").click()
        scenario = page.locator(f'[data-scenario="{scenario_id}"]')
        scenario.locator('[data-action="play"]').click()
        page.wait_for_selector(
            f'[data-scenario="{scenario_id}"] [data-report]', state="attached", timeout=30_000
        )
    page.locator(f'[data-scenario="{ids[-1]}"] [data-action="show-everything"]').click()

    violations = axe_violations(page)
    assert violations == [], describe(violations)
    assert watch.failed == []
