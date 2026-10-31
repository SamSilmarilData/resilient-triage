"""Scenario: the frozen definition of one catalogued scenario run (ADR-009).

A scenario is data: the request (query, scope), the model's scripted replies, what Statuspage
reads, the scenario setup done before the run, per-run overrides, the page copy (chapter, title,
caption, setup notes) and the outcome its run must show. `ScenarioFactory` turns one into a
private, runnable `ScenarioRun`; `public_view()` is all the page may see of it.
"""

import re
from dataclasses import dataclass, field

from resilient_triage.llm import ScriptEntry
from resilient_triage.schemas.api import ScenarioView
from resilient_triage.schemas.incident import DegradationStatus, IncidentTriageReport
from resilient_triage.schemas.telemetry import ChaosConfig, StatuspageSummary
from resilient_triage.trace.fields import (
    ModelOutcome,
    ModelRole,
    ReadOutcome,
    StoreOutcome,
    TriageOutcomeKind,
)

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
    # Each must fail in this scenario: "statuspage" needs statuspage=None, "chaos" a
    # cluster_sentinel that always fails.
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
class ExpectedOutcome:
    """What a scenario run must show: its path through the graph and the answer's key facts."""

    outcome: TriageOutcomeKind
    # The answer's degradation status; None when shed (no answer).
    degradation: DegradationStatus | None = None
    nodes: tuple[str, ...] = ()  # graph nodes visited, in order
    routes: tuple[tuple[str, str], ...] = ()  # (router's source node, route key), in order
    model_calls: tuple[tuple[ModelRole, ModelOutcome], ...] = ()
    telemetry: tuple[tuple[str, ReadOutcome], ...] = ()  # run-time reads, not setup's
    breakers_tripped: tuple[str, ...] = ()  # the answer's circuit_breakers_tripped
    approval_raised: tuple[int, ...] = ()  # action indexes reconcile made require approval
    confidence: float | None = None  # the answer's confidence_score
    cache_store: StoreOutcome | None = None  # None when the run never reached the store
    deadline_exceeded: bool = False


@dataclass(frozen=True)
class Chapter:
    """A group of scenarios on the page, shown under its title in catalogue order."""

    id: str
    title: str


@dataclass(frozen=True)
class Scenario:
    """One catalogued scenario: request, scripted replies and Statuspage, setup and overrides."""

    id: str
    chapter: str  # the id of the Chapter it is shown under
    title: str
    query: str
    scope: str | None
    # The model's replies in call order, each with an optional delay (a ScriptedReply);
    # once they run out, the default valid report.
    script: tuple[ScriptEntry, ...]
    # What Statuspage reads; None means every Statuspage read fails (an unavailable source).
    statuspage: StatuspageSummary | None
    caption: str
    expected_outcome: ExpectedOutcome
    # Cluster Sentinel failing or slow, set on the run's private ChaosManager; None: healthy.
    cluster_sentinel: ChaosConfig | None = None
    setup: Setup = field(default_factory=Setup)
    overrides: Overrides = field(default_factory=Overrides)
    # What is true before the run, in words for the page (the setup and overrides, the faults).
    setup_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _SCENARIO_ID.match(self.id):
            raise ValueError(f"Scenario id {self.id!r} is not an identifier")
        sentinel = self.cluster_sentinel
        if sentinel is not None and 0 < sentinel.failure_rate < 1:
            raise ValueError("A random Cluster Sentinel failure_rate would not repeat exactly")
        failing = {
            "statuspage": self.statuspage is None,
            "chaos": sentinel is not None
            and sentinel.active
            and (sentinel.inject_503 or sentinel.failure_rate == 1),
        }
        for source in self.setup.tripped_sources:
            if not failing.get(source, False):
                raise ValueError(f"Scenario setup cannot trip {source!r}: it does not fail here")

    def public_view(self) -> ScenarioView:
        """The page copy for GET /scenarios: no script, query or expected outcome."""
        return ScenarioView(
            id=self.id,
            chapter=self.chapter,
            title=self.title,
            caption=self.caption,
            setup_notes=self.setup_notes,
        )
