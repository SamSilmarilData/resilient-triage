"""Circuit breaker: project-owned async breaker interface; the only module importing pybreaker."""

import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import pybreaker
from pydantic import BaseModel, Field

from resilient_triage.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BreakerOpen(Exception):
    """Raised when a circuit breaker refuses a call because it is open."""


class CircuitBreakerInfo(BaseModel):
    """Snapshot representation of circuit breaker health and state."""

    name: str
    state: str = Field(..., description="Current state: 'closed', 'open', or 'half-open'")
    fail_counter: int = Field(..., description="Consecutive failure count")
    fail_max: int = Field(..., description="Failure threshold to trip breaker")
    is_open: bool = Field(..., description="True if breaker is currently open")
    last_failure_time: datetime | None = None
    last_state_change: datetime | None = None
    last_failure_reason: str | None = None


class CircuitBreakerMetricsListener(pybreaker.CircuitBreakerListener):
    """Custom listener tracking state changes, metrics, and failure diagnostics."""

    def __init__(self) -> None:
        self.last_state_change: datetime = datetime.now(UTC)
        self.last_failure_time: datetime | None = None
        self.last_failure_reason: str | None = None
        self.last_success_time: datetime | None = None
        self.total_trips: int = 0

    def state_change(
        self,
        cb: pybreaker.CircuitBreaker,
        old_state: pybreaker.CircuitBreakerState | None,
        new_state: pybreaker.CircuitBreakerState,
    ) -> None:
        self.last_state_change = datetime.now(UTC)
        old_name = old_state.name if old_state else "none"
        new_name = new_state.name if new_state else "unknown"

        if new_name == pybreaker.STATE_OPEN:
            self.total_trips += 1
            logger.error(
                "CIRCUIT BREAKER TRIPPED OPEN: '%s' tripped from %s to %s (consecutive failures: %d/%d)",
                cb.name,
                old_name,
                new_name,
                cb.fail_counter,
                cb.fail_max,
            )
        elif new_name == pybreaker.STATE_HALF_OPEN:
            logger.warning(
                "CIRCUIT BREAKER HALF-OPEN: '%s' probing recovery from %s to %s",
                cb.name,
                old_name,
                new_name,
            )
        else:
            logger.info(
                "CIRCUIT BREAKER RECOVERED: '%s' closed from %s to %s",
                cb.name,
                old_name,
                new_name,
            )

    def failure(self, cb: pybreaker.CircuitBreaker, exc: BaseException) -> None:
        self.last_failure_time = datetime.now(UTC)
        self.last_failure_reason = str(exc)
        logger.warning(
            "Circuit breaker '%s' recorded failure (%d/%d): %s",
            cb.name,
            cb.fail_counter,
            cb.fail_max,
            exc,
        )

    def success(self, cb: pybreaker.CircuitBreaker) -> None:
        self.last_success_time = datetime.now(UTC)


class AsyncCircuitBreaker:
    """Async circuit breaker; the project's interface over pybreaker.

    Bypasses pybreaker's legacy Tornado-dependent call_async while preserving
    pybreaker's state machine, listeners, thread-safety, and storage options.
    Callers see only `call`, `name`, `state`, `info()` and `reset()`, and catch
    `BreakerOpen` when the breaker refuses a call.

    `call` raises `BreakerOpen` only when it refuses a call without running the guarded
    function: the breaker is open and its reset timeout has not elapsed, or it is half-open
    and another call is already the trial. An `Exception` from the guarded function counts as
    a failure and propagates unchanged, including on the call whose failure trips the
    breaker open. A non-`Exception` `BaseException` (e.g. `CancelledError`) propagates and
    records nothing. In half-open only the current trial's outcome is recorded; see
    docs/resilience.md.
    """

    def __init__(
        self, breaker: pybreaker.CircuitBreaker, listener: CircuitBreakerMetricsListener
    ) -> None:
        self._breaker = breaker
        self._listener = listener
        # Token of the half-open trial call in flight, if any; guarded by `_breaker._lock`.
        self._trial: object | None = None

    @property
    def name(self) -> str:
        """Breaker name as registered."""
        return self._breaker.name or ""

    @property
    def state(self) -> str:
        """Current state: 'closed', 'open' or 'half-open'."""
        return str(self._breaker.current_state)

    def info(self) -> CircuitBreakerInfo:
        """Produce a diagnostic snapshot of breaker status and metrics."""
        state = self.state
        return CircuitBreakerInfo(
            name=self.name,
            state=state,
            fail_counter=self._breaker.fail_counter,
            fail_max=self._breaker.fail_max,
            is_open=state == pybreaker.STATE_OPEN,
            last_failure_time=self._listener.last_failure_time,
            last_state_change=self._listener.last_state_change,
            last_failure_reason=self._listener.last_failure_reason,
        )

    async def call(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Await `func(*args, **kwargs)` under the breaker rules (see class docstring)."""
        # 1. Check state before execution; in half-open, claim the single trial slot or refuse.
        trial: object | None = None
        with self._breaker._lock:
            if str(self._breaker.current_state) == pybreaker.STATE_OPEN:
                timeout = timedelta(seconds=self._breaker.reset_timeout)
                opened_at = self._breaker._state_storage.opened_at
                if opened_at and datetime.now(UTC) < opened_at + timeout:
                    raise BreakerOpen("Timeout not elapsed yet, circuit breaker still open")
                # Timeout elapsed: transition to half-open trial probe
                self._breaker.half_open()

            if str(self._breaker.current_state) == pybreaker.STATE_HALF_OPEN:
                if self._trial is not None:
                    raise BreakerOpen("Half-open trial call in progress, circuit breaker refusing")
                trial = self._trial = object()

            for listener in self._breaker.listeners:
                listener.before_call(self._breaker, func, *args, **kwargs)

        try:
            return await self._run(trial, func, *args, **kwargs)
        finally:
            if trial is not None:
                # Free the slot whatever the outcome; a cancelled trial leaves the breaker
                # half-open, so the next caller becomes the new trial.
                with self._breaker._lock:
                    if self._trial is trial:
                        self._trial = None

    async def _run(
        self,
        trial: object | None,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Await func and record its outcome on the wrapped breaker."""
        try:
            # 2. Execute async coroutine
            result = await func(*args, **kwargs)
        except Exception as exc:
            # 3. Handle error inside lock. Only `Exception` counts: a non-Exception BaseException
            # such as CancelledError (run deadline, client disconnect) propagates untouched and
            # records nothing.
            with self._breaker._lock:
                if self._may_record(trial):
                    # pybreaker raises its own error on the tripping failure. The breaker is now
                    # open, but fn did run, so the caller gets fn's own error; BreakerOpen is
                    # only for refused calls.
                    with contextlib.suppress(pybreaker.CircuitBreakerError):
                        self._breaker.state._handle_error(exc, reraise=False)
            raise
        else:
            # 4. Handle success inside lock
            with self._breaker._lock:
                if self._may_record(trial):
                    self._breaker.state._handle_success()
            return result

    def _may_record(self, trial: object | None) -> bool:
        """Whether a call's outcome may be recorded; caller holds `_breaker._lock`."""
        # In half-open only the current trial settles the breaker; stragglers record nothing.
        if str(self._breaker.current_state) != pybreaker.STATE_HALF_OPEN:
            return True
        return trial is not None and self._trial is trial

    def reset(self) -> None:
        """Manually close the breaker, reset failure counters and free any trial slot."""
        with self._breaker._lock:
            # A trial still running after reset is stale: it no longer holds the slot.
            self._trial = None
            self._breaker.close()


def force_open_for_tests(breaker: AsyncCircuitBreaker) -> None:
    """Test-only: trip `breaker` open immediately, without any failing calls."""
    breaker._breaker.open()


class CircuitBreakerRegistry:
    """Central registry managing named circuit breaker instances."""

    def __init__(self) -> None:
        self._breakers: dict[str, AsyncCircuitBreaker] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        name: str,
        fail_max: int | None = None,
        reset_timeout: int | None = None,
    ) -> AsyncCircuitBreaker:
        """Retrieve existing circuit breaker by name or create a new one.

        Args:
            name: Unique name for the circuit breaker.
            fail_max: Optional failure threshold override before tripping.
            reset_timeout: Optional cooldown duration in seconds before HALF_OPEN probe.

        Returns:
            AsyncCircuitBreaker: Active or newly instantiated breaker instance.
        """
        with self._lock:
            if name in self._breakers:
                return self._breakers[name]

            effective_fail_max = (
                fail_max if fail_max is not None else settings.circuit_breaker_fail_max
            )
            effective_reset_timeout = (
                reset_timeout
                if reset_timeout is not None
                else settings.circuit_breaker_reset_timeout
            )

            listener = CircuitBreakerMetricsListener()
            sync_breaker = pybreaker.CircuitBreaker(
                fail_max=effective_fail_max,
                reset_timeout=effective_reset_timeout,
                name=name,
                listeners=[listener],
            )
            async_breaker = AsyncCircuitBreaker(sync_breaker, listener)
            self._breakers[name] = async_breaker
            return async_breaker

    def get(self, name: str) -> AsyncCircuitBreaker | None:
        """Get an existing circuit breaker by name.

        Args:
            name: Circuit breaker identifier.

        Returns:
            AsyncCircuitBreaker | None: Breaker instance if found, else None.
        """
        with self._lock:
            return self._breakers.get(name)

    def get_all_statuses(self) -> dict[str, CircuitBreakerInfo]:
        """Get snapshots of all registered circuit breakers.

        Returns:
            dict[str, CircuitBreakerInfo]: Mapping of breaker name to diagnostic info.
        """
        with self._lock:
            return {name: cb.info() for name, cb in self._breakers.items()}

    def reset_all(self) -> None:
        """Reset all registered breakers to closed state."""
        with self._lock:
            for cb in self._breakers.values():
                cb.reset()


# Global registry singleton
circuit_breaker_registry = CircuitBreakerRegistry()
