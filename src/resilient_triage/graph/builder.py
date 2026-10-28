"""LangGraph cyclic state machine construction and compilation."""

import logging
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from resilient_triage.config import settings
from resilient_triage.graph.nodes import (
    analyze_query_node,
    collect_telemetry_node,
    compensate_blindspots_node,
    degraded_fallback_node,
    fallback_report_node,
    finalize_node,
    generate_report_node,
    refine_reflection_node,
    self_repair_node,
    synthesize_telemetry_node,
    validate_report_node,
)
from resilient_triage.runtime import TriageDeps
from resilient_triage.schemas.state import TriageState

logger = logging.getLogger(__name__)


class BoundedMemorySaver(MemorySaver):
    """LangGraph MemorySaver checkpointer with bounded thread capacity and FIFO/LRU eviction."""

    def __init__(self, max_threads: int = 1000) -> None:
        super().__init__()
        self.max_threads = max_threads

    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        """Persist checkpoint while enforcing max thread storage capacity."""
        thread_id = (
            config.get("configurable", {}).get("thread_id") if hasattr(config, "get") else None
        )
        if thread_id and thread_id not in self.storage and len(self.storage) >= self.max_threads:
            oldest_thread = next(iter(self.storage))
            self.storage.pop(oldest_thread, None)
            if hasattr(self, "writes") and isinstance(self.writes, dict):
                self.writes.pop(oldest_thread, None)  # type: ignore[call-overload]
        return super().put(config, checkpoint, metadata, new_versions)


def route_after_telemetry(state: TriageState) -> Literal["compensate", "synthesize"]:
    """Route to compensatory degraded branch if any targeted circuit breaker is OPEN.

    Args:
        state: Current LangGraph execution state.

    Returns:
        Literal["compensate", "synthesize"]: Next destination node key.
    """
    tripped = state.get("tripped_breakers", [])
    if tripped:
        logger.warning("Routing to compensatory degraded branch for tripped breakers: %s", tripped)
        return "compensate"

    return "synthesize"


def route_after_validation(
    state: TriageState,
) -> Literal["finalize", "repair", "refine", "fallback"]:
    """Route based on Pydantic validation status, confidence score, and retry limits.

    Args:
        state: Current LangGraph execution state.

    Returns:
        Literal["finalize", "repair", "refine", "fallback"]: Next destination node key.
    """
    final_report = state.get("final_report")

    if final_report is not None:
        # Check for confidence-driven reflection (only once)
        if final_report.confidence_score < 0.60 and state.get("refinement_attempts", 0) == 0:
            logger.info(
                "Report valid but confidence low (%.2f). Routing to refine_reflection.",
                final_report.confidence_score,
            )
            return "refine"
        return "finalize"

    # Validation failed
    repair_attempts = state.get("repair_attempts", 0)
    if repair_attempts < settings.max_self_repair_retries:
        logger.info(
            "Validation failed (attempt %d/%d). Routing to self_repair cycle.",
            repair_attempts,
            settings.max_self_repair_retries,
        )
        return "repair"

    logger.warning(
        "Self-repair retries exhausted (%d/%d). Routing to fallback_report.",
        repair_attempts,
        settings.max_self_repair_retries,
    )
    return "fallback"


def build_triage_graph(
    enable_checkpointing: bool = True, max_checkpoint_threads: int = 1000
) -> CompiledStateGraph[TriageState, TriageDeps, TriageState, TriageState]:
    """Build and compile the LangGraph cyclic state graph with BoundedMemorySaver checkpointing.

    Args:
        enable_checkpointing: If True, attaches a checkpointer to preserve thread state.
        max_checkpoint_threads: Maximum number of distinct threads to retain in memory.

    Returns:
        CompiledStateGraph: Runnable LangGraph workflow engine.
    """
    graph = StateGraph(TriageState, context_schema=TriageDeps)

    # 1. Register nodes
    graph.add_node("analyze_query", analyze_query_node)
    graph.add_node("collect_telemetry", collect_telemetry_node)
    graph.add_node("compensate_blindspots", compensate_blindspots_node)
    graph.add_node("degraded_fallback", degraded_fallback_node)
    graph.add_node("synthesize_telemetry", synthesize_telemetry_node)
    graph.add_node("generate_report", generate_report_node)
    graph.add_node("validate_report", validate_report_node)
    graph.add_node("self_repair", self_repair_node)
    graph.add_node("refine_reflection", refine_reflection_node)
    graph.add_node("fallback_report", fallback_report_node)
    graph.add_node("finalize_report", finalize_node)

    # 2. Main flow edges
    graph.add_edge(START, "analyze_query")
    graph.add_edge("analyze_query", "collect_telemetry")

    # 3. Telemetry conditional routing (Circuit breaker check)
    graph.add_conditional_edges(
        "collect_telemetry",
        route_after_telemetry,
        {
            "compensate": "compensate_blindspots",
            "synthesize": "synthesize_telemetry",
        },
    )

    # 4. Compensatory and degraded flow
    graph.add_edge("compensate_blindspots", "degraded_fallback")
    graph.add_edge("degraded_fallback", "synthesize_telemetry")

    # 5. Synthesis & generation
    graph.add_edge("synthesize_telemetry", "generate_report")
    graph.add_edge("generate_report", "validate_report")

    # 6. Validation conditional routing (Self-repair, Refine, Fallback, Finalize)
    graph.add_conditional_edges(
        "validate_report",
        route_after_validation,
        {
            "finalize": "finalize_report",
            "repair": "self_repair",
            "refine": "refine_reflection",
            "fallback": "fallback_report",
        },
    )

    # 7. Cycles
    graph.add_edge("self_repair", "generate_report")
    graph.add_edge("refine_reflection", "generate_report")

    # 8. Termination
    graph.add_edge("fallback_report", "finalize_report")
    graph.add_edge("finalize_report", END)

    checkpointer = (
        BoundedMemorySaver(max_threads=max_checkpoint_threads) if enable_checkpointing else None
    )
    return graph.compile(checkpointer=checkpointer)


triage_graph = build_triage_graph(enable_checkpointing=True)
