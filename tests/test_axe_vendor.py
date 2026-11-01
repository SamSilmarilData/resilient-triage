"""The browser tier's vendored axe-core (#117): pinned by sha256, licensed, never shipped."""

from repo_scripts import STATIC_DIR, TEST_VENDOR_DIR, load_script, vendored_axe

vendor = load_script("vendor")


def test_test_vendor_files_match_their_manifest():
    """Every file under tests/vendor/ is listed, present and matches its pinned sha256."""
    assert vendor.verify(TEST_VENDOR_DIR) == []


def test_axe_core_is_vendored_with_its_licenses():
    """axe-core ships its script, its MPL-2.0 LICENSE and its third-party notices."""
    axe = vendored_axe()
    assert axe.package["license"] == "MPL-2.0"
    assert axe.package["license_file"] == "LICENSE"
    files = {f["path"] for f in axe.package["files"]}
    assert files == {"axe.min.js", "LICENSE", "LICENSE-3RD-PARTY.txt"}
    assert axe.path.read_bytes().startswith(f"/*! axe v{axe.version}".encode())


def test_axe_core_is_never_served_by_the_app():
    """The static tree holds no copy of axe-core: tests inject it, the app never serves it."""
    assert not [p for p in STATIC_DIR.rglob("*") if "axe" in p.name.lower()]
