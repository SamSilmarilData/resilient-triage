"""ScriptedAdapter: the test fake and zero-config simulation behind the LLM gateway."""

import json
from asyncio import sleep  # module-level name, so tests can zero declared delays
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import BaseMessage

from resilient_triage.llm.failures import ProviderFailure
from resilient_triage.llm.gateway import Completion, QuotaHeaders

SIMULATION_LABEL = "Zero-Config SRE Simulation"

# The answer once the script runs out: a valid report, so the simulation triages end to end.
DEFAULT_REPORT_JSON = json.dumps(
    {
        "summary": "GitHub Actions workflows experiencing elevated failure rates",
        "severity": "HIGH",
        "root_cause_analysis": "Upstream webhook processing queue saturation",
        "affected_services": [
            {
                "service_name": "Actions Runner",
                "impact_level": "Major Outage",
                "details": "Job pickup delayed by >15 minutes",
            }
        ],
        "telemetry_signals": [
            {
                "source": "statuspage:global",
                "status": "major",
                "latency_ms": 320.0,
                "error_rate": 0.5,
                "raw_snippet": "GitHub: Major Service Outage",
            }
        ],
        "recommended_actions": [
            {
                "priority": "P0",
                "action": ("Inspect Actions webhook runner health: kubectl get pods -n actions"),
                "target_system": "Actions Controller",
                "action_type": "diagnostic",
                "requires_approval": False,
            },
            {
                "priority": "P1",
                "action": "Scale runner replica pool by 50%",
                "target_system": "Runner Deployment",
                "action_type": "remediative",
                "requires_approval": True,
            },
        ],
        "degradation_status": "HEALTHY",
        "circuit_breakers_tripped": [],
        "confidence_score": 0.90,
    }
)


@dataclass(frozen=True)
class ScriptedReply:
    """A script entry that arrives after a declared delay, a real asyncio.sleep in the call."""

    reply: str | ProviderFailure
    delay_s: float = 0.0

    def __post_init__(self) -> None:
        if self.delay_s < 0:
            raise ValueError("A scripted reply delay cannot be negative")


ScriptEntry = str | ProviderFailure | ScriptedReply


class ScriptedAdapter:
    """Returns script entries in order, then DEFAULT_REPORT_JSON; records each call's model."""

    def __init__(
        self,
        script: Sequence[ScriptEntry] = (),
        *,
        quota: QuotaHeaders | None = None,
    ) -> None:
        self.script: list[ScriptEntry] = list(script)
        self.calls: list[str] = []
        self.quota = quota if quota is not None else QuotaHeaders()

    def label(self, primary: str) -> str:
        """The zero-config simulation's label, whatever the primary model."""
        return SIMULATION_LABEL

    def quota_headers(self, model: str) -> QuotaHeaders:
        """Return the scripted quota headers, the same for every model."""
        return self.quota

    async def call(self, model: str, messages: list[BaseMessage]) -> Completion | ProviderFailure:
        """Return the next script entry, after its delay (a str becomes a Completion)."""
        index = len(self.calls)
        self.calls.append(model)
        entry = self.script[index] if index < len(self.script) else DEFAULT_REPORT_JSON
        if isinstance(entry, ScriptedReply):
            await sleep(entry.delay_s)
            entry = entry.reply
        return Completion(text=entry, model=model) if isinstance(entry, str) else entry

    async def aclose(self) -> None:
        """Nothing to release."""
