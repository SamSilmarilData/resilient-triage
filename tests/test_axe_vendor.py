"""The browser tier's vendored axe-core (#117): pinned by sha256, licensed, never shipped."""

import json

from repo_scripts import REPO_ROOT, STATIC_DIR, load_script

TEST_VENDOR_DIR = REPO_ROOT / "tests" / "vendor"


vendor = load_script("vendor")


def test_test_vendor_files_match_their_manifest():
    """Every file under tests/vendor/ is listed, present and matches its pinned sha256."""
    assert vendor.verify(TEST_VENDOR_DIR) == []


def test_axe_core_is_vendored_with_its_licenses():
    """axe-core ships its script, its MPL-2.0 LICENSE and its third-party notices."""
    manifest = json.loads((TEST_VENDOR_DIR / "manifest.json").read_text(encoding="utf-8"))
    (axe,) = [p for p in manifest["packages"] if p["name"] == "axe-core"]
    assert axe["license"] == "MPL-2.0"
    assert axe["license_file"] == "LICENSE"
    assert {f["path"] for f in axe["files"]} == {"axe.min.js", "LICENSE", "LICENSE-3RD-PARTY.txt"}
    script = TEST_VENDOR_DIR / f"axe-core@{axe['version']}" / "axe.min.js"
    assert script.read_bytes().startswith(f"/*! axe v{axe['version']}".encode())


def test_axe_core_is_never_served_by_the_app():
    """The static tree holds no copy of axe-core: tests inject it, the app never serves it."""
    assert not [p for p in STATIC_DIR.rglob("*") if "axe" in p.name.lower()]
