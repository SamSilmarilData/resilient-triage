"""Free-play run budget: a sliding 60 s window of admitted graph runs, per process."""

from fakes import FakeClock
from resilient_triage.triage.budget import RunBudget


def test_admits_up_to_the_limit_then_refuses_with_seconds_until_the_oldest_ages_out() -> None:
    clock = FakeClock()
    budget = RunBudget(runs_per_minute=2, clock=clock)

    assert budget.admit()
    clock.now += 10.2
    assert budget.admit()
    clock.now += 5.0

    assert not budget.admit()
    assert budget.retry_after_s() == 45  # oldest run at t=0 ages out at t=60; now t=15.2


def test_the_window_slides_rather_than_resetting_on_the_minute() -> None:
    clock = FakeClock()
    budget = RunBudget(runs_per_minute=2, clock=clock)
    assert budget.admit()
    clock.now += 30.0
    assert budget.admit()

    clock.now += 30.0  # the first run has aged out; the second has not
    assert budget.admit()
    assert not budget.admit()
    assert budget.retry_after_s() == 30


def test_a_refused_run_does_not_count() -> None:
    clock = FakeClock()
    budget = RunBudget(runs_per_minute=1, clock=clock)
    assert budget.admit()
    for _ in range(5):
        clock.now += 1.0
        assert not budget.admit()

    clock.now += 55.0
    assert budget.admit()


def test_retry_after_is_at_least_one_second() -> None:
    clock = FakeClock()
    budget = RunBudget(runs_per_minute=1, clock=clock)
    assert budget.admit()
    clock.now += 59.99

    assert not budget.admit()
    assert budget.retry_after_s() == 1
