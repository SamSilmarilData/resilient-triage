"""Node: collect_telemetry - Concurrently gathers targeted telemetry signals."""

import asyncio
from typing import Any

from langgraph.runtime import Runtime

from resilient_triage.graph.deps import TriageDeps
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.state import TriageState
from resilient_triage.schemas.telemetry import SIGNAL_STATUS_UNAVAILABLE


async def collect_telemetry_node(
    state: TriageState, runtime: Runtime[TriageDeps]
) -> dict[str, Any]:
    """Execute concurrent telemetry collection on dynamically targeted tools."""
    telemetry = runtime.context.telemetry
    target_tools = state.get("target_tools", ["statuspage", "chaos"])
    tasks = []
    task_names = []

    if "statuspage" in target_tools:
        tasks.append(telemetry.statuspage.fetch_signals())
        task_names.append("statuspage")

    if "chaos" in target_tools:
        tasks.append(
            telemetry.chaos.fetch_signals(
                service_hint=state.get("service_filter"),
                query=state.get("query"),
            )
        )
        task_names.append("chaos")

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    aggregated_signals: list[TelemetrySignal] = []
    failed_sources: set[str] = set()
    for name, res in zip(task_names, raw_results, strict=True):
        if isinstance(res, Exception):
            failed_sources.add(name)
            aggregated_signals.append(
                TelemetrySignal(
                    source=f"{name}:error",
                    status=SIGNAL_STATUS_UNAVAILABLE,
                    raw_snippet=f"Telemetry fetch failed: {res}",
                )
            )
        elif isinstance(res, list):
            aggregated_signals.extend(res)
            if any(sig.status == SIGNAL_STATUS_UNAVAILABLE for sig in res):
                failed_sources.add(name)

    statuses = telemetry.breakers.get_all_statuses()
    cb_status = {name: info.state for name, info in statuses.items()}
    tripped_breakers = [name for name in task_names if name in statuses and statuses[name].is_open]
    # A tripped breaker is already counted as such; it is not also an unavailable source.
    unavailable_sources = [
        name for name in task_names if name in failed_sources and name not in tripped_breakers
    ]

    return {
        "telemetry_signals": [s.model_dump() for s in aggregated_signals],
        "circuit_breaker_status": cb_status,
        "tripped_breakers": tripped_breakers,
        "unavailable_sources": unavailable_sources,
    }
