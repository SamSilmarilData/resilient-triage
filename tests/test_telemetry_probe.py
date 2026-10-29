"""Guarded telemetry probe policy, exercised through fake Telemetry sources (no HTTP mocks)."""

import asyncio
import time

import pytest

from resilient_triage.config import settings
from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry, force_open_for_tests
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.telemetry import SIGNAL_STATUS_CIRCUIT_OPEN, SIGNAL_STATUS_UNAVAILABLE
from resilient_triage.telemetry.probe import TelemetryProbe

QUERY = "payments failing"
SCOPE = "billing"


class FakeSource:
    """A Telemetry source that returns one OK signal, or raises / sleeps when told to."""

    def __init__(
        self,
        source_id: str,
        *,
        fail: bool = False,
        delay_s: float = 0.0,
    ) -> None:
        self.id = source_id
        self.fail = fail
        self.delay_s = delay_s
        self.reads: list[tuple[str, str | None]] = []

    async def read(self, query: str, scope: str | None) -> list[TelemetrySignal]:
        self.reads.append((query, scope))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError(f"{self.id} exploded")
        return [TelemetrySignal(source=f"{self.id}:ok", status="OK")]

    def degraded_label(self, query: str, scope: str | None) -> str:
        return f"{self.id}:{scope}"


def _probe(*sources: FakeSource, fail_max: int = 3) -> TelemetryProbe:
    registry = CircuitBreakerRegistry()
    for source in sources:
        registry.get_or_create(source.id, fail_max=fail_max)
    return TelemetryProbe(sources, breakers=registry)


async def test_a_successful_read_returns_its_signals_and_no_failed_sources() -> None:
    source = FakeSource("alpha")
    probe = _probe(source)

    reading = await probe.collect(frozenset({"alpha"}), QUERY, SCOPE)

    assert [s.source for s in reading.signals] == ["alpha:ok"]
    assert reading.tripped_breakers == frozenset()
    assert reading.unavailable_sources == frozenset()
    assert reading.breaker_states == {"alpha": "closed"}
    assert source.reads == [(QUERY, SCOPE)]


async def test_a_raising_source_is_unavailable_with_one_degraded_signal() -> None:
    probe = _probe(FakeSource("alpha", fail=True))

    reading = await probe.collect(frozenset({"alpha"}), QUERY, SCOPE)

    assert reading.unavailable_sources == frozenset({"alpha"})
    assert reading.tripped_breakers == frozenset()
    [degraded] = reading.signals
    assert degraded.source == "alpha:billing"
    assert degraded.status == SIGNAL_STATUS_UNAVAILABLE
    assert degraded.error_rate == 1.0
    assert "alpha exploded" in (degraded.raw_snippet or "")


async def test_fail_max_failures_trip_the_source_on_the_tripping_read_and_the_next() -> None:
    probe = _probe(FakeSource("alpha", fail=True), fail_max=3)
    targets = frozenset({"alpha"})

    early = [await probe.collect(targets, QUERY, SCOPE) for _ in range(2)]
    tripping = await probe.collect(targets, QUERY, SCOPE)
    after = await probe.collect(targets, QUERY, SCOPE)

    assert all(r.unavailable_sources == targets for r in early)
    for reading in (tripping, after):
        assert reading.tripped_breakers == targets
        assert reading.unavailable_sources == frozenset()
        assert [s.status for s in reading.signals] == [SIGNAL_STATUS_CIRCUIT_OPEN]
        assert reading.breaker_states == {"alpha": "open"}
    # The tripping read still reports the real failure it saw.
    assert "alpha exploded" in (tripping.signals[0].raw_snippet or "")


async def test_a_refused_read_never_calls_the_source() -> None:
    source = FakeSource("alpha")
    probe = _probe(source)
    force_open_for_tests(probe.breakers.get_or_create("alpha"))

    started = time.monotonic()
    reading = await probe.collect(frozenset({"alpha"}), QUERY, SCOPE)

    assert source.reads == []
    assert reading.tripped_breakers == frozenset({"alpha"})
    [degraded] = reading.signals
    assert degraded.source == "alpha:billing"
    assert degraded.status == SIGNAL_STATUS_CIRCUIT_OPEN
    assert degraded.error_rate == 1.0
    assert "tripped OPEN" in (degraded.raw_snippet or "")
    assert degraded.latency_ms < 10.0
    assert time.monotonic() - started < 0.1


async def test_non_targeted_sources_are_never_read_and_never_counted_as_failed() -> None:
    targeted = FakeSource("alpha")
    tripped = FakeSource("beta", fail=True)
    probe = _probe(targeted, tripped)
    force_open_for_tests(probe.breakers.get_or_create("beta"))

    reading = await probe.collect(frozenset({"alpha"}), QUERY, SCOPE)

    assert tripped.reads == []
    assert reading.tripped_breakers == frozenset()
    assert reading.unavailable_sources == frozenset()
    assert [s.source for s in reading.signals] == ["alpha:ok"]
    assert reading.breaker_states == {"alpha": "closed", "beta": "open"}


async def test_each_failed_source_adds_exactly_one_degraded_signal_with_its_label() -> None:
    probe = _probe(FakeSource("alpha", fail=True), FakeSource("beta"), FakeSource("gamma"))
    force_open_for_tests(probe.breakers.get_or_create("gamma"))

    reading = await probe.collect(frozenset({"alpha", "beta", "gamma"}), QUERY, SCOPE)

    assert [(s.source, s.status) for s in reading.signals] == [
        ("alpha:billing", SIGNAL_STATUS_UNAVAILABLE),
        ("beta:ok", "OK"),
        ("gamma:billing", SIGNAL_STATUS_CIRCUIT_OPEN),
    ]
    assert reading.unavailable_sources == frozenset({"alpha"})
    assert reading.tripped_breakers == frozenset({"gamma"})


async def test_sources_are_read_concurrently() -> None:
    probe = _probe(FakeSource("alpha", delay_s=0.2), FakeSource("beta", delay_s=0.2))

    started = time.monotonic()
    reading = await probe.collect(frozenset({"alpha", "beta"}), QUERY, SCOPE)

    assert time.monotonic() - started < 0.35
    assert len(reading.signals) == 2


async def test_cancelling_collect_records_nothing_on_any_breaker() -> None:
    probe = _probe(FakeSource("alpha", delay_s=5), FakeSource("beta", delay_s=5))

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(probe.collect(frozenset({"alpha", "beta"}), QUERY, SCOPE), 0.05)

    statuses = probe.breakers.get_all_statuses()
    assert {name: info.fail_counter for name, info in statuses.items()} == {"alpha": 0, "beta": 0}


async def test_a_source_past_the_deadline_is_unavailable_and_records_one_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telemetry_source_deadline_s", 0.5)
    probe = _probe(FakeSource("alpha", delay_s=5))

    started = time.monotonic()
    reading = await probe.collect(frozenset({"alpha"}), QUERY, SCOPE)

    assert time.monotonic() - started < 0.5 + 0.3
    assert reading.unavailable_sources == frozenset({"alpha"})
    assert [s.status for s in reading.signals] == [SIGNAL_STATUS_UNAVAILABLE]
    alpha = probe.breakers.get_all_statuses()["alpha"]
    assert alpha.fail_counter == 1
    assert alpha.state == "closed"


def test_building_the_probe_creates_one_breaker_per_source() -> None:
    registry = CircuitBreakerRegistry()

    TelemetryProbe([FakeSource("alpha"), FakeSource("beta")], breakers=registry)

    statuses = registry.get_all_statuses()
    assert set(statuses) == {"alpha", "beta"}
    assert statuses["alpha"].fail_max == settings.circuit_breaker_fail_max


async def test_guarded_runs_a_read_through_the_source_breaker_and_raises_its_failure() -> None:
    probe = _probe(FakeSource("alpha"))

    async def failing() -> str:
        raise RuntimeError("boom")

    async def ok() -> str:
        return "pong"

    assert await probe.guarded("alpha", ok) == "pong"
    with pytest.raises(RuntimeError, match="boom"):
        await probe.guarded("alpha", failing)
    assert probe.breakers.get_all_statuses()["alpha"].fail_counter == 1
