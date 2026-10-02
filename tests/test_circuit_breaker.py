"""Tests for AsyncCircuitBreaker, metrics tracking, and CircuitBreakerRegistry."""

import time
import pytest
from pybreaker import CircuitBreakerError

from resilient_triage.resilience.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitBreakerRegistry,
)


@pytest.mark.asyncio
async def test_circuit_breaker_stays_closed_on_success():
    """Verify healthy invocations do not increment failure counters and keep breaker closed."""
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create(name="healthy_service", fail_max=3, reset_timeout=30)

    async def healthy_task():
        return "ok"

    for _ in range(5):
        res = await cb.call(healthy_task)
        assert res == "ok"

    assert cb.current_state == "closed"
    assert cb.fail_counter == 0
    assert not cb.is_open


@pytest.mark.asyncio
async def test_circuit_breaker_trips_open_on_3_failures():
    """Verify that exactly 3 consecutive failures trip the breaker to OPEN."""
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create(name="failing_service", fail_max=3, reset_timeout=30)

    invocation_count = 0

    async def failing_task():
        nonlocal invocation_count
        invocation_count += 1
        raise RuntimeError("Downstream service crashed")

    # Invocations 1 and 2 fail normally
    for i in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(failing_task)
        assert cb.current_state == "closed"
        assert cb.fail_counter == i + 1

    # Invocation 3 reaches threshold: trips breaker to OPEN
    with pytest.raises((RuntimeError, CircuitBreakerError)):
        await cb.call(failing_task)

    assert cb.current_state == "open"
    assert cb.is_open

    # Invocation 4: FAIL FAST without calling failing_task
    start = time.monotonic()
    with pytest.raises(CircuitBreakerError) as exc_info:
        await cb.call(failing_task)
    duration_ms = (time.monotonic() - start) * 1000.0

    # Ensure failing_task was NOT executed on the 4th call
    assert invocation_count == 3
    # Fail fast duration < 5ms
    assert duration_ms < 5.0
    assert "open" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_circuit_breaker_registry_snapshots_and_reset():
    """Verify registry tracks multiple named breakers and supports full reset."""
    registry = CircuitBreakerRegistry()
    cb1 = registry.get_or_create("service_alpha", fail_max=3)
    cb2 = registry.get_or_create("service_beta", fail_max=3)

    async def fail():
        raise ValueError("boom")

    for _ in range(3):
        try:
            await cb1.call(fail)
        except Exception:
            pass

    statuses = registry.get_all_statuses()
    assert "service_alpha" in statuses
    assert "service_beta" in statuses
    assert statuses["service_alpha"].is_open is True
    assert statuses["service_beta"].is_open is False

    registry.reset_all()
    assert cb1.current_state == "closed"
    assert cb1.fail_counter == 0
