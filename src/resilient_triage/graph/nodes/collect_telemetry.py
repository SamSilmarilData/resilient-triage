"""Node: collect_telemetry - Reads the targeted Telemetry sources through the guarded probe."""

from typing import Any

from langgraph.runtime import Runtime

from resilient_triage.graph.deps import TriageDeps
from resilient_triage.schemas.state import TriageState


async def collect_telemetry_node(
    state: TriageState, runtime: Runtime[TriageDeps]
) -> dict[str, Any]:
    """Collect every targeted source in one probe call and record what failed."""
    probe = runtime.context.telemetry
    # No targeting yet means every source.
    targets = state.get("targeted_sources", probe.source_ids)
    reading = await probe.collect(
        frozenset(targets), state.get("query", ""), state.get("service_filter")
    )
    return {
        "telemetry_signals": [s.model_dump() for s in reading.signals],
        "circuit_breaker_status": reading.breaker_states,
        "tripped_breakers": [i for i in probe.source_ids if i in reading.tripped_breakers],
        "unavailable_sources": [i for i in probe.source_ids if i in reading.unavailable_sources],
    }
