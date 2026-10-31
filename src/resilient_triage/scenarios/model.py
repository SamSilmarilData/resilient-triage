"""Scenario: the frozen definition of one catalogued scenario run (ADR-009).

A scenario is data: the request (query, scope), the model's scripted replies, what Statuspage
reads, the scenario setup done before the run, per-run overrides, and the caption and outcome
the page shows. `ScenarioFactory` turns one into a private, runnable `ScenarioRun`.
"""

import re
from dataclasses import dataclass

from resilient_triage.llm import ProviderFailure
from resilient_triage.schemas.telemetry import StatuspageSummary
from resilient_triage.trace.fields import TriageOutcomeKind

# Same shape as the trace's Identifier, since the id travels on trace_started.
_SCENARIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


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

    def __post_init__(self) -> None:
        if not _SCENARIO_ID.match(self.id):
            raise ValueError(f"Scenario id {self.id!r} is not an identifier")
