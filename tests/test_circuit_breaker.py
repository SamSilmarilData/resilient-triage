"""Tests for AsyncCircuitBreaker, metrics tracking, and CircuitBreakerRegistry."""

import asyncio
import time

import pytest

from resilient_triage.resilience.circuit_breaker import (
    AsyncCircuitBreaker,
    BreakerOpen,
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

    assert cb.state == "closed"
    assert cb.info().fail_counter == 0
    assert not cb.info().is_open


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
        assert cb.state == "closed"
        assert cb.info().fail_counter == i + 1

    # Invocation 3 reaches threshold: trips breaker to OPEN
    with pytest.raises(RuntimeError):
        await cb.call(failing_task)

    assert cb.state == "open"
    assert cb.info().is_open

    # Invocation 4: FAIL FAST without calling failing_task
    start = time.monotonic()
    with pytest.raises(BreakerOpen) as exc_info:
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
    registry.get_or_create("service_beta", fail_max=3)

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
    assert cb1.state == "closed"
    assert cb1.info().fail_counter == 0


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_recovery_and_failure(monkeypatch):
    """Verify half-open probe execution: trial success closes breaker, trial failure re-opens it."""
    registry = CircuitBreakerRegistry()
    # 0.1s timeout for fast test execution
    cb = registry.get_or_create("recovery_test_service", fail_max=2, reset_timeout=0.1)

    fail_count = 0

    async def flaky_service():
        nonlocal fail_count
        if fail_count < 2:
            fail_count += 1
            raise RuntimeError("Outage ongoing")
        return "recovered"

    # 1. Cause 2 failures to trip breaker to OPEN
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(flaky_service)

    assert cb.state == "open"

    # 2. Before timeout, fast-fails without executing flaky_service
    with pytest.raises(BreakerOpen):
        await cb.call(flaky_service)
    assert fail_count == 2

    # 3. Wait for reset timeout (0.12s)
    await asyncio.sleep(0.12)

    # 4. Trial call executes: flaky_service now succeeds
    result = await cb.call(flaky_service)
    assert result == "recovered"
    assert cb.state == "closed"
    assert cb.info().fail_counter == 0

    # 5. Now test trial call failure in half-open
    cb2 = registry.get_or_create("half_open_fail_service", fail_max=1, reset_timeout=0.1)

    async def permanently_dead_service():
        raise RuntimeError("Service is dead")

    with pytest.raises(RuntimeError):
        await cb2.call(permanently_dead_service)

    assert cb2.state == "open"
    await asyncio.sleep(0.12)

    # Trial call fails
    with pytest.raises(RuntimeError):
        await cb2.call(permanently_dead_service)

    # Breaker re-opens
    assert cb2.state == "open"


async def _cancel_in_flight(cb: AsyncCircuitBreaker) -> None:
    """Start a slow guarded call, cancel it mid-flight, assert the caller sees CancelledError."""

    async def slow_task() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(cb.call(slow_task))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelled_call_does_not_count_as_failure():
    """A cancelled in-flight call leaves a fail_max=1 breaker closed with no recorded failure."""
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create(name="cancelled_service", fail_max=1, reset_timeout=30)

    await _cancel_in_flight(cb)

    assert cb.state == "closed"
    assert cb.info().fail_counter == 0


async def test_cancelled_call_does_not_reset_consecutive_failures():
    """With fail_max=3, the sequence fail, fail, cancel, fail opens the breaker."""
    registry = CircuitBreakerRegistry()
    cb = registry.get_or_create(name="cancel_between_failures", fail_max=3, reset_timeout=30)

    async def failing_task():
        raise RuntimeError("Downstream service crashed")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(failing_task)

    await _cancel_in_flight(cb)
    assert cb.info().fail_counter == 2

    with pytest.raises(RuntimeError, match="Downstream service crashed"):
        await cb.call(failing_task)
    assert cb.state == "open"


async def test_tripping_call_reraises_the_original_error():
    """The call whose failure trips the breaker gets fn's own exception, not BreakerOpen."""
    cb = CircuitBreakerRegistry().get_or_create(name="trip_reraise", fail_max=1, reset_timeout=30)
    original = RuntimeError("Downstream service crashed")
    reached = 0

    async def failing_task() -> None:
        nonlocal reached
        reached += 1
        raise original

    with pytest.raises(RuntimeError) as exc_info:
        await cb.call(failing_task)
    assert exc_info.value is original
    assert cb.state == "open"

    # Only a refused call, one that never reaches fn, raises BreakerOpen.
    with pytest.raises(BreakerOpen):
        await cb.call(failing_task)
    assert reached == 1


async def _half_open_breaker(name: str) -> AsyncCircuitBreaker:
    """Return a breaker whose reset timeout has elapsed, so the next call is the half-open trial."""
    cb = CircuitBreakerRegistry().get_or_create(name=name, fail_max=1, reset_timeout=0.05)

    async def failing_task() -> None:
        raise RuntimeError("Outage ongoing")

    with pytest.raises(RuntimeError, match="Outage ongoing"):
        await cb.call(failing_task)
    assert cb.state == "open"
    await asyncio.sleep(0.07)
    return cb


async def test_half_open_lets_exactly_one_concurrent_call_through():
    """With the breaker half-open, N concurrent calls: one reaches fn, the rest get BreakerOpen."""
    cb = await _half_open_breaker("single_trial_service")
    release = asyncio.Event()
    reached = 0

    async def probe():
        nonlocal reached
        reached += 1
        await release.wait()
        return "recovered"

    trial = asyncio.create_task(cb.call(probe))
    await asyncio.sleep(0.01)
    assert cb.state == "half-open"

    others = [asyncio.create_task(cb.call(probe)) for _ in range(4)]
    # Rejected callers must not wait on the trial: bound the wait so a regression fails, not hangs.
    done, pending = await asyncio.wait(others, timeout=0.5)
    for task in pending:
        task.cancel()
    assert not pending
    results = [task.exception() for task in done]
    assert all(isinstance(r, BreakerOpen) for r in results)
    assert reached == 1

    release.set()
    assert await trial == "recovered"
    assert cb.state == "closed"


async def test_failed_half_open_trial_reopens_and_restarts_timeout():
    """A failed trial re-opens the breaker, and the next caller is refused without reaching fn."""
    cb = await _half_open_breaker("failed_trial_service")
    reached = 0

    async def failing_probe():
        nonlocal reached
        reached += 1
        raise RuntimeError("Still down")

    with pytest.raises(RuntimeError, match="Still down"):
        await cb.call(failing_probe)
    assert cb.state == "open"

    with pytest.raises(BreakerOpen):
        await cb.call(failing_probe)
    assert reached == 1


async def test_cancelled_half_open_trial_frees_the_slot():
    """A cancelled trial leaves the breaker half-open, and the next caller becomes the new trial."""
    cb = await _half_open_breaker("cancelled_trial_service")

    await _cancel_in_flight(cb)
    assert cb.state == "half-open"

    async def probe():
        return "recovered"

    assert await cb.call(probe) == "recovered"
    assert cb.state == "closed"


async def test_callers_racing_in_as_reset_timeout_elapses_get_exactly_one_trial():
    """N callers hitting an open breaker whose timeout just elapsed: exactly one reaches fn."""
    cb = await _half_open_breaker("racing_trial_service")
    assert cb.state == "open"  # timeout elapsed, but no call has claimed the trial yet
    release = asyncio.Event()
    reached = 0

    async def probe() -> str:
        nonlocal reached
        reached += 1
        await release.wait()
        return "recovered"

    tasks = [asyncio.create_task(cb.call(probe)) for _ in range(5)]
    # Bound the wait so a regression fails rather than hangs; only the trial should be pending.
    done, pending = await asyncio.wait(tasks, timeout=0.5)
    assert len(pending) == 1
    assert all(isinstance(task.exception(), BreakerOpen) for task in done)
    assert reached == 1

    release.set()
    assert await asyncio.wait_for(pending.pop(), timeout=0.5) == "recovered"
    assert cb.state == "closed"


async def _straggler_and_trial(
    name: str, straggler_fails: bool
) -> tuple[
    AsyncCircuitBreaker, asyncio.Event, "asyncio.Task[str]", asyncio.Event, "asyncio.Task[str]"
]:
    """Start a call while closed, trip the breaker, then start the half-open trial; both block."""
    cb = CircuitBreakerRegistry().get_or_create(name=name, fail_max=1, reset_timeout=0.05)
    straggler_release = asyncio.Event()
    trial_release = asyncio.Event()

    async def straggler() -> str:
        await straggler_release.wait()
        if straggler_fails:
            raise RuntimeError("Straggler failed")
        return "straggler"

    async def trial() -> str:
        await trial_release.wait()
        return "recovered"

    async def failing_task() -> None:
        raise RuntimeError("Outage ongoing")

    straggler_task = asyncio.create_task(cb.call(straggler))
    await asyncio.sleep(0.01)
    with pytest.raises(RuntimeError, match="Outage ongoing"):
        await cb.call(failing_task)
    await asyncio.sleep(0.07)
    trial_task = asyncio.create_task(cb.call(trial))
    await asyncio.sleep(0.01)
    assert cb.state == "half-open"
    return cb, straggler_release, straggler_task, trial_release, trial_task


async def test_straggler_success_during_half_open_does_not_close_breaker():
    """A call started while closed that succeeds during the trial records nothing."""
    cb, straggler_release, straggler_task, trial_release, trial_task = await _straggler_and_trial(
        "straggler_success_service", straggler_fails=False
    )

    straggler_release.set()
    assert await asyncio.wait_for(straggler_task, timeout=0.5) == "straggler"
    assert cb.state == "half-open"

    trial_release.set()
    assert await asyncio.wait_for(trial_task, timeout=0.5) == "recovered"
    assert cb.state == "closed"


async def test_straggler_failure_during_half_open_does_not_reopen_breaker():
    """A call started while closed that fails mid-trial raises its own error, records nothing."""
    cb, straggler_release, straggler_task, trial_release, trial_task = await _straggler_and_trial(
        "straggler_failure_service", straggler_fails=True
    )

    straggler_release.set()
    with pytest.raises(RuntimeError, match="Straggler failed"):
        await asyncio.wait_for(straggler_task, timeout=0.5)
    assert cb.state == "half-open"

    trial_release.set()
    assert await asyncio.wait_for(trial_task, timeout=0.5) == "recovered"
    assert cb.state == "closed"


async def test_reset_during_trial_frees_the_slot_for_the_next_half_open_cycle():
    """After a mid-trial reset, the next cycle admits a new trial the stale one can't free."""
    cb = await _half_open_breaker("reset_during_trial_service")
    stale_release = asyncio.Event()
    new_release = asyncio.Event()
    reached: list[str] = []

    async def stale_probe() -> str:
        reached.append("stale")
        await stale_release.wait()
        return "stale"

    async def new_probe() -> str:
        reached.append("new")
        await new_release.wait()
        return "recovered"

    async def failing_task() -> None:
        raise RuntimeError("Outage ongoing")

    stale = asyncio.create_task(cb.call(stale_probe))
    await asyncio.sleep(0.01)
    cb.reset()
    with pytest.raises(RuntimeError, match="Outage ongoing"):
        await cb.call(failing_task)
    await asyncio.sleep(0.07)

    new = asyncio.create_task(cb.call(new_probe))
    await asyncio.sleep(0.01)
    assert reached == ["stale", "new"]
    assert cb.state == "half-open"

    # The stale trial returning must neither settle the breaker nor free the new trial's slot.
    stale_release.set()
    assert await asyncio.wait_for(stale, timeout=0.5) == "stale"
    assert cb.state == "half-open"
    with pytest.raises(BreakerOpen):
        await asyncio.wait_for(cb.call(new_probe), timeout=0.5)
    assert reached == ["stale", "new"]

    new_release.set()
    assert await asyncio.wait_for(new, timeout=0.5) == "recovered"
    assert cb.state == "closed"
