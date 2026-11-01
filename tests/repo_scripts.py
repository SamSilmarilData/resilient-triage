"""Repo paths the tests share, and a loader for `scripts/*.py`, which isn't a package."""

import importlib.util
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
# The page's static files, as the app serves them at /static.
STATIC_DIR = REPO_ROOT / "src" / "resilient_triage" / "api" / "static"
# Test-only vendored files (axe-core), pinned by their own manifest and never served.
TEST_VENDOR_DIR = REPO_ROOT / "tests" / "vendor"


def load_script(name: str) -> ModuleType:
    """Execute `scripts/<name>.py` and return it as a module."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class VendoredAxe:
    """axe-core as tests/vendor/manifest.json pins it."""

    package: dict[str, Any]  # its manifest entry
    path: Path  # its axe.min.js
    version: str


@cache
def vendored_axe() -> VendoredAxe:
    """Look up axe-core in the test-side manifest: its entry, script path and pinned version."""
    vendor = load_script("vendor")
    packages = vendor.load_manifest(TEST_VENDOR_DIR)["packages"]
    (package,) = [p for p in packages if p["name"] == "axe-core"]
    script = TEST_VENDOR_DIR / vendor.package_dir(package) / "axe.min.js"
    return VendoredAxe(package, script, package["version"])
