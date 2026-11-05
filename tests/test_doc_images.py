"""Every image the README and docs reference exists in the repo (#123).

Screenshots are produced by hand with `scripts/screenshots.py`; this keeps a moved, renamed or
deleted capture from leaving a broken image on GitHub.
"""

import re
from pathlib import Path

import pytest
from repo_scripts import REPO_ROOT

# Markdown files whose images GitHub renders: the README and every doc under docs/.
DOCUMENTS = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").rglob("*.md"))]
# <img src="...">, <source srcset="..."> and Markdown ![alt](path) references.
HTML_SRC = re.compile(r"""\b(?:src|srcset)\s*=\s*["']([^"']+)["']""")
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def image_references(text: str) -> list[str]:
    """The local image paths a Markdown document references, in order; URLs are left out."""
    found: list[str] = []
    for match in HTML_SRC.finditer(text):
        # A srcset lists comma-separated candidates, each a URL and an optional descriptor.
        found.extend(candidate.split()[0] for candidate in match.group(1).split(",") if candidate)
    found.extend(match.group(1) for match in MARKDOWN_IMAGE.finditer(text))
    return [ref for ref in found if not re.match(r"^[a-z][a-z0-9+.-]*:", ref, re.IGNORECASE)]


def test_image_references_reads_html_srcset_and_markdown_and_skips_urls():
    """The extractor finds every local image form the docs use and ignores remote ones."""
    text = (
        '<picture><source media="(prefers-color-scheme: dark)" srcset="img/a-dark.png">'
        '<img src="img/a-light.png" alt="A"></picture>\n'
        "![B](docs/b.svg) [![badge](https://img.shields.io/x.svg)](https://example.com)\n"
        '<img src="https://example.com/c.png">'
    )
    assert image_references(text) == ["img/a-dark.png", "img/a-light.png", "docs/b.svg"]


def test_the_readme_shows_the_page_screenshots_in_both_themes():
    """The README embeds the explorable page's captures, light and dark, with <picture>."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    refs = image_references(readme)
    assert "<picture>" in readme
    assert any(ref.startswith("docs/img/") and ref.endswith("-light.png") for ref in refs)
    assert any(ref.startswith("docs/img/") and ref.endswith("-dark.png") for ref in refs)


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_referenced_image_exists(document: Path):
    """Each local image a document references resolves to a file, relative to the document."""
    refs = image_references(document.read_text(encoding="utf-8"))
    missing = [ref for ref in refs if not (document.parent / ref.split("#")[0]).is_file()]
    assert missing == [], f"{document.relative_to(REPO_ROOT)} references missing images: {missing}"
