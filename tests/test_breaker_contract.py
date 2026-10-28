"""Contract tests: pybreaker internals AsyncCircuitBreaker relies on, and its import boundary."""

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from resilient_triage.resilience.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitBreakerRegistry,
    force_open_for_tests,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BREAKER_MODULE = REPO_ROOT / "src" / "resilient_triage" / "resilience" / "circuit_breaker.py"


def _wrapped(cb: AsyncCircuitBreaker) -> Any:
    """Return the pybreaker object the adapter wraps."""
    return cb._breaker


def test_pybreaker_exposes_reentrant_lock():
    """The adapter guards state transitions with `_lock` used as a context manager."""
    breaker = _wrapped(CircuitBreakerRegistry().get_or_create("contract_lock", fail_max=3))
    lock = breaker._lock
    with lock, lock:  # re-entrant: the adapter may nest acquisitions
        pass


def test_pybreaker_opened_at_is_timezone_aware():
    """`_state_storage.opened_at` is UTC-aware after open (pybreaker 1.2.0 stored it naive)."""
    cb = CircuitBreakerRegistry().get_or_create("contract_opened_at", fail_max=3)
    force_open_for_tests(cb)
    opened_at = _wrapped(cb)._state_storage.opened_at
    assert isinstance(opened_at, datetime)
    assert opened_at.tzinfo is not None
    assert abs(datetime.now(UTC) - opened_at) < timedelta(seconds=5)


def test_pybreaker_half_open_transitions_state():
    """`half_open()` moves an open breaker to 'half-open'."""
    cb = CircuitBreakerRegistry().get_or_create("contract_half_open", fail_max=3)
    force_open_for_tests(cb)
    _wrapped(cb).half_open()
    assert cb.state == "half-open"


def test_pybreaker_handle_error_accepts_reraise_false_and_counts():
    """`state._handle_error(exc, reraise=False)` records a failure, no raise below fail_max."""
    cb = CircuitBreakerRegistry().get_or_create("contract_handle_error", fail_max=3)
    handle_error = _wrapped(cb).state._handle_error
    assert "reraise" in inspect.signature(handle_error).parameters
    handle_error(RuntimeError("boom"), reraise=False)
    assert cb.info().fail_counter == 1
    assert cb.state == "closed"


def test_pybreaker_handle_success_resets_failures():
    """`state._handle_success()` clears the consecutive-failure counter."""
    cb = CircuitBreakerRegistry().get_or_create("contract_handle_success", fail_max=3)
    _wrapped(cb).state._handle_error(RuntimeError("boom"), reraise=False)
    _wrapped(cb).state._handle_success()
    assert cb.info().fail_counter == 0


def _imports_pybreaker(path: Path) -> bool:
    """Return True if the Python file at `path` imports pybreaker."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".")[0] == "pybreaker" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pybreaker":
            return True
    return False


def test_pybreaker_is_imported_only_by_the_breaker_module():
    """No module in src/ or tests/ other than the breaker module imports pybreaker."""
    files = [*(REPO_ROOT / "src").rglob("*.py"), *(REPO_ROOT / "tests").rglob("*.py")]
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in files
        if path != BREAKER_MODULE and _imports_pybreaker(path)
    ]
    assert offenders == []
    assert _imports_pybreaker(BREAKER_MODULE)
