"""Capture the README's screenshots of the explorable page, light and dark (#123).

Run by hand after a visible page change, from the checkout with the `browser` extra installed
and `playwright install chromium` done:

    .venv/bin/python scripts/screenshots.py

It serves the app on loopback over the browser tier's scripted runtime (tests/browser/harness.py:
scripted model replies, canned Statuspage, deterministic embedder, emptied cache), so it needs no
Groq key or network, and every page request off the app's own origin is aborted. Each capture is
written as `<name>-light.png` and `<name>-dark.png` to docs/img/ (or `--out`); the README embeds
both with <picture>, and tests/test_doc_images.py checks every referenced image exists.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parents[1]
# The browser harness and its helpers live with the tests, which aren't a package.
sys.path[:0] = [str(REPO_ROOT / "tests" / "browser"), str(REPO_ROOT / "tests")]

from harness import (  # noqa: E402
    CATALOGUE_READY,
    GRAPH_READY,
    GuardedPages,
    LiveServer,
    open_shell,
    serve_live_app,
    wait_for_run,
)

if TYPE_CHECKING:
    from playwright.sync_api import FloatRect, Page

DEFAULT_OUT = REPO_ROOT / "docs" / "img"
SCHEMES = ("light", "dark")
WIDTH = 1280
DEVICE_SCALE = 2
# CSS pixels of page kept around a clipped capture.
PADDING = 24
# The scenario whose run stands in for the state-machine figure: it takes the self-repair loop.
SCENARIO = "self-repair"
FREE_PLAY_ROOT = "#free-play-root"
FREE_PLAY_QUESTION = "GitHub Actions workflows failing with 503s on every push"


@dataclass(frozen=True)
class Capture:
    """One screenshot: its file stem, viewport height and how to stage it.

    `stage` drives the page into the state to capture and returns the selectors whose union is
    captured, the first scrolled to the top; none captures the viewport as loaded.
    """

    name: str
    height: int
    stage: Callable[[Page], tuple[str, ...]]


def stage_page(page: Page) -> tuple[str, ...]:
    """The top of the page: the hook, with the chapters and the idle live trace below it."""
    page.wait_for_selector(CATALOGUE_READY, state="attached")
    page.wait_for_selector(GRAPH_READY, state="attached")
    return ()


def stage_scenario_run(page: Page) -> tuple[str, ...]:
    """A finished scenario run: the open scenario with its report, and the pinned live trace."""
    page.wait_for_selector(CATALOGUE_READY, state="attached")
    page.wait_for_selector(GRAPH_READY, state="attached")
    page.locator(f"#scenario-{SCENARIO}-toggle").click()
    page.locator(f'[data-scenario="{SCENARIO}"] [data-action="play"]').click()
    wait_for_run(page)
    return (f'[data-scenario="{SCENARIO}"]', "#guided .stage")


def stage_free_play(page: Page) -> tuple[str, ...]:
    """A finished free-play run: the form, the model line and the run's graph and timeline."""
    panel = page.locator(FREE_PLAY_ROOT)
    panel.get_by_label("Your incident").fill(FREE_PLAY_QUESTION)
    panel.get_by_role("button", name="Triage").click()
    wait_for_run(page, root=FREE_PLAY_ROOT)
    return ("#free-play-title", FREE_PLAY_ROOT)


CAPTURES = (
    Capture("page", 800, stage_page),
    Capture("scenario-run", 1600, stage_scenario_run),
    Capture("free-play", 2200, stage_free_play),
)


def clip_around(page: Page, selectors: tuple[str, ...]) -> FloatRect:
    """Scroll the first selector to the top; return the viewport box around them all, padded."""
    page.locator(selectors[0]).evaluate("(el) => el.scrollIntoView({block: 'start'})")
    page.evaluate(f"window.scrollBy(0, -{PADDING})")
    boxes = []
    for selector in selectors:
        box = page.locator(selector).bounding_box()
        if box is None:
            raise SystemExit(f"nothing to capture: {selector} isn't visible")
        boxes.append(box)
    viewport = page.viewport_size
    assert viewport is not None
    left = max(min(box["x"] for box in boxes) - PADDING, 0)
    # A tighter top margin keeps the text above the first selector out of the capture.
    top = max(min(box["y"] for box in boxes) - PADDING // 3, 0)
    right = min(max(box["x"] + box["width"] for box in boxes) + PADDING, viewport["width"])
    bottom = max(box["y"] + box["height"] for box in boxes) + PADDING
    if bottom > viewport["height"]:
        raise SystemExit(f"{selectors} end at {bottom:.0f} px; raise the capture's height")
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def capture_all(server: LiveServer, out: Path) -> list[Path]:
    """Write every capture in both themes to `out`; return the files written."""
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pages = GuardedPages(browser, server)
        try:
            for capture in CAPTURES:
                for scheme in SCHEMES:
                    # Each capture starts from a fresh cache, so free play is a full run.
                    server.run(server.runtime.cache.clear())
                    page, _ = pages.open(
                        color_scheme=scheme,
                        reduced_motion="reduce",
                        viewport={"width": WIDTH, "height": capture.height},
                        device_scale_factor=DEVICE_SCALE,
                    )
                    open_shell(page)
                    selectors = capture.stage(page)
                    path = out / f"{capture.name}-{scheme}.png"
                    if selectors:
                        page.screenshot(path=path, clip=clip_around(page, selectors))
                    else:
                        page.screenshot(path=path)
                    written.append(path)
                    page.context.close()
        finally:
            pages.close()
            browser.close()
    problems = pages.blocked() + pages.page_errors()
    if problems:
        raise SystemExit(f"the page left its origin or threw: {problems}")
    return written


def main(argv: list[str] | None = None) -> int:
    """Serve the app on a scripted runtime and write the README's captures."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="directory to write to")
    args = parser.parse_args(argv)
    with serve_live_app() as server:
        for path in capture_all(server, args.out):
            print(path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
