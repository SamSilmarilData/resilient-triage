"""Node: collect_telemetry - Concurrently gathers targeted telemetry signals."""

import asyncio
from typing import Any

from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.state import TriageState
from resilient_triage.telemetry.chaos import ChaosClient
from resilient_triage.telemetry.statuspage import StatuspageClient


async def collect_telemetry_node(state: TriageState) -> dict[str, Any]:
    """Execute concurrent telemetry collection on dynamically targeted tools."""
    target_tools = state.get("target_tools", ["statuspage", "chaos"])
    tasks = []
    task_names = []

    if "statuspage" in target_tools:
        sp_client = StatuspageClient()
        tasks.append(sp_client.fetch_signals())
        task_names.append("statuspage")

    if "chaos" in target_tools:
        ch_client = ChaosClient()
        tasks.append(ch_client.fetch_signals())
        task_names.append("chaos")

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    aggregated_signals: list[TelemetrySignal] = []
    for name, res in zip(task_names, raw_results):
        if isinstance(res, Exception):
            aggregated_signals.append(
                TelemetrySignal(
                    source=f"{name}:error",
                    status="UNAVAILABLE",
                    raw_snippet=f"Telemetry fetch failed: {res}",
                )
            )
        elif isinstance(res, list):
            aggregated_signals.extend(res)

    statuses = circuit_breaker_registry.get_all_statuses()
    cb_status = {name: info.state for name, info in statuses.items()}

    return {
        "telemetry_signals": [s.model_dump() for s in aggregated_signals],
        "circuit_breaker_status": cb_status,
    }
