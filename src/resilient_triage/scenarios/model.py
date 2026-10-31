"""Scenario: the frozen definition of one catalogued scenario run (ADR-009).

A scenario is data: the request (query, scope), the model's scripted replies, what Statuspage
reads, the scenario setup done before the run, per-run overrides, and the caption and outcome
the page shows. `ScenarioFactory` turns one into a private, runnable `ScenarioRun`.
"""

import re
from dataclasses import dataclass, field

from resilient_triage.llm import ProviderFailure
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.telemetry import StatuspageSummary
from resilient_triage.trace.fields import TriageOutcomeKind

# Same shape as the trace's Identifier, since the id travels on trace_started.
_SCENARIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


@dataclass(frozen=True)
class CacheSeed:
    """A scenario seed: a query and report stored in the run's private cache before the run."""

    query: str
    report: IncidentTriageReport
    scope: str | None = None


@dataclass(frozen=True)
class Setup:
    """Scenario setup: what is already true before the run starts, all traced before t = 0."""

    # Sources whose private breaker is tripped by fail_max real failed reads before the run.
    # Each must fail in this scenario (statuspage=None for "statuspage").
    tripped_sources: tuple[str, ...] = ()
    # Stored through the cache's real store() path, so the run's lookup embeds and compares.
    seeds: tuple[CacheSeed, ...] = ()


@dataclass(frozen=True)
class HeldSlots:
    """A private run slot pool whose every slot is held before the run (the shed request)."""

    capacity: int
    queue_timeout_s: float

    def __post_init__(self) -> None:
        if self.capacity < 1 or self.queue_timeout_s <= 0:
            raise ValueError("Held slots need at least one slot and a positive queue timeout")


@dataclass(frozen=True)
class Overrides:
    """Scenario-only overrides, for this run only and shown as scenario setup."""

    # A short run deadline in seconds, in place of triage_run_deadline_s.
    run_deadline_s: float | None = None
    # A private, pre-filled slot pool in place of the shared scenario slot pool.
    held_slots: HeldSlots | None = None

    def __post_init__(self) -> None:
        if self.run_deadline_s is not None and self.run_deadline_s <= 0:
            raise ValueError("A run deadline override must be positive")


@dataclass(frozen=True)
class Scenario:
    """One catalogued scenario: request, scripted replies and Statuspage, setup and overrides."""

    id: str
    query: str
    scope: str | None
    # The model's replies in call order; once they run out, the default valid report.
    script: tuple[str | ProviderFailure, ...]
    # What Statuspage reads; None means every Statuspage read fails (an unavailable source).
    statuspage: StatuspageSummary | None
    caption: str
    expected_outcome: TriageOutcomeKind
    setup: Setup = field(default_factory=Setup)
    overrides: Overrides = field(default_factory=Overrides)

    def __post_init__(self) -> None:
        if not _SCENARIO_ID.match(self.id):
            raise ValueError(f"Scenario id {self.id!r} is not an identifier")
        for source in self.setup.tripped_sources:
            if source != "statuspage":
                raise ValueError(f"Scenario setup can only trip 'statuspage', not {source!r}")
            if self.statuspage is not None:
                raise ValueError("Tripping 'statuspage' needs a failing Statuspage (None)")
