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

    def __post_init__(self) -> None:
        if not _SCENARIO_ID.match(self.id):
            raise ValueError(f"Scenario id {self.id!r} is not an identifier")
        for source in self.setup.tripped_sources:
            if source != "statuspage":
                raise ValueError(f"Scenario setup can only trip 'statuspage', not {source!r}")
            if self.statuspage is not None:
                raise ValueError("Tripping 'statuspage' needs a failing Statuspage (None)")
