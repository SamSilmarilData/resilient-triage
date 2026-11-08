"""Every module under src/ imports cleanly as a fresh interpreter's first import."""

import os
import pkgutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import resilient_triage

SRC = Path(__file__).resolve().parents[1] / "src"


def _modules() -> list[str]:
    found = pkgutil.walk_packages(resilient_triage.__path__, prefix="resilient_triage.")
    return ["resilient_triage", *sorted(info.name for info in found)]


def _import_error(module: str) -> str | None:
    """Return the stderr of importing module first in a new interpreter, or None if it imports."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")])),
    }
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return None if result.returncode == 0 else result.stderr.strip().splitlines()[-1]


def test_every_module_imports_first_in_a_fresh_interpreter() -> None:
    """Import order hides cycles in-process, so each module gets its own interpreter."""
    modules = _modules()
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        errors = dict(zip(modules, pool.map(_import_error, modules), strict=True))
    failures = {module: error for module, error in errors.items() if error}
    assert not failures, failures
