"""Circuit breaker implementation with pybreaker and async support."""

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

import pybreaker
from pydantic import BaseModel, Field

from resilient_triage.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


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
        self.last_state_change: datetime = datetime.now(timezone.utc)
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
        self.last_state_change = datetime.now(timezone.utc)
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
        self.last_failure_time = datetime.now(timezone.utc)
        self.last_failure_reason = str(exc)
        logger.warning(
            "Circuit breaker '%s' recorded failure (%d/%d): %s",
            cb.name,
            cb.fail_counter,
            cb.fail_max,
            exc,
        )

    def success(self, cb: pybreaker.CircuitBreaker) -> None:
        self.last_success_time = datetime.now(timezone.utc)


class AsyncCircuitBreaker:
    """Thread-safe async adapter for pybreaker.CircuitBreaker.

    Bypasses pybreaker's legacy Tornado-dependent call_async while preserving
    100% of pybreaker's state machine, listeners, thread-safety, and storage options.
    """

    def __init__(self, breaker: pybreaker.CircuitBreaker, listener: CircuitBreakerMetricsListener) -> None:
        self.breaker = breaker
        self.listener = listener

    @property
    def name(self) -> str:
        return self.breaker.name

    @property
    def current_state(self) -> str:
        return str(self.breaker.current_state)

    @property
    def is_open(self) -> bool:
        return self.current_state == pybreaker.STATE_OPEN

    @property
    def fail_counter(self) -> int:
        return self.breaker.fail_counter

    @property
    def fail_max(self) -> int:
        return self.breaker.fail_max

    def get_info(self) -> CircuitBreakerInfo:
        """Produce a diagnostic snapshot of breaker status."""
        return CircuitBreakerInfo(
            name=self.name,
            state=self.current_state,
            fail_counter=self.fail_counter,
            fail_max=self.fail_max,
            is_open=self.is_open,
            last_failure_time=self.listener.last_failure_time,
            last_state_change=self.listener.last_state_change,
            last_failure_reason=self.listener.last_failure_reason,
        )

    async def call(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute async function guarded by circuit breaker rules."""
        # 1. Check state before execution (raises CircuitBreakerError if OPEN and timeout not elapsed)
        with self.breaker._lock:
            self.breaker.state.before_call(func, *args, **kwargs)
            for listener in self.breaker.listeners:
                listener.before_call(self.breaker, func, *args, **kwargs)

        try:
            # 2. Execute async coroutine
            result = await func(*args, **kwargs)
        except BaseException as exc:
            # 3. Handle error inside lock
            with self.breaker._lock:
                self.breaker.state._handle_error(exc)
            raise
        else:
            # 4. Handle success inside lock
            with self.breaker._lock:
                self.breaker.state._handle_success()
            return result

    def close(self) -> None:
        """Manually close the circuit breaker and reset failure counters."""
        self.breaker.close()


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
        """Retrieve existing circuit breaker by name or create a new one."""
        with self._lock:
            if name in self._breakers:
                return self._breakers[name]

            effective_fail_max = fail_max if fail_max is not None else settings.circuit_breaker_fail_max
            effective_reset_timeout = (
                reset_timeout if reset_timeout is not None else settings.circuit_breaker_reset_timeout
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
        """Get an existing circuit breaker by name."""
        with self._lock:
            return self._breakers.get(name)

    def get_all_statuses(self) -> dict[str, CircuitBreakerInfo]:
        """Get snapshots of all registered circuit breakers."""
        with self._lock:
            return {name: cb.get_info() for name, cb in self._breakers.items()}

    def reset_all(self) -> None:
        """Reset all registered breakers to closed state."""
        with self._lock:
            for cb in self._breakers.values():
                cb.close()


# Global registry singleton
circuit_breaker_registry = CircuitBreakerRegistry()
