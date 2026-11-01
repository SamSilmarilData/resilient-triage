"""Repo paths the tests share, and a loader for `scripts/*.py`, which isn't a package."""

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
# The page's static files, as the app serves them at /static.
STATIC_DIR = REPO_ROOT / "src" / "resilient_triage" / "api" / "static"


def load_script(name: str) -> ModuleType:
    """Execute `scripts/<name>.py` and return it as a module."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
