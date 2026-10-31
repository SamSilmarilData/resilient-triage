"""LangGraph cyclic state machine construction and compilation."""

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from resilient_triage.config import settings
from resilient_triage.graph.deps import TriageDeps
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
from resilient_triage.graph.nodes.validate_report import self_repair_exhausted
from resilient_triage.schemas.state import TriageState
from resilient_triage.trace.events import (
    NodeFinished,
    NodeStarted,
    Reconciled,
    RouteDecided,
)
from resilient_triage.trace.fields import NodeStatus
from resilient_triage.trace.recorder import TraceRecorder, record_event

logger = logging.getLogger(__name__)


class _TracedNode(Protocol):
    """A graph node as LangGraph calls it: the state, and the run's Runtime by keyword."""

    def __call__(
        self, state: TriageState, *, runtime: Runtime[TriageDeps]
    ) -> Awaitable[dict[str, Any]]: ...


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


def route_after_generation(state: TriageState) -> Literal["validate", "finalize", "fallback"]:
    """Route a provider failure past self-repair: keep a pre-refine report, else fall back."""
    failure = state.get("provider_failure")
    if failure is not None:
        if state.get("final_report") is not None:
            logger.warning(
                "LLM provider failure (%s) in a refine round. Finalizing the pre-refine report.",
                failure,
            )
            return "finalize"
        logger.warning("LLM provider failure (%s). Routing to fallback_report.", failure)
        return "fallback"
    return "validate"


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
    if not self_repair_exhausted(repair_attempts):
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


def build_triage_graph() -> CompiledStateGraph[TriageState, TriageDeps, TriageState, TriageState]:
    """Compile the triage graph with no checkpointer: run state lives only in ainvoke (ADR-007)."""
    graph = StateGraph(TriageState, context_schema=TriageDeps)
    # Filled from get_graph() once compiled; each traced node reads it at run time.
    routes: dict[tuple[str, str], str] = {}

    # 1. Register nodes, each wrapped to record its trace events (ADR-008)
    nodes: dict[str, Callable[..., Any]] = {
        "analyze_query": analyze_query_node,
        "collect_telemetry": collect_telemetry_node,
        "compensate_blindspots": compensate_blindspots_node,
        "degraded_fallback": degraded_fallback_node,
        "synthesize_telemetry": synthesize_telemetry_node,
        "generate_report": generate_report_node,
        "validate_report": validate_report_node,
        "self_repair": self_repair_node,
        "refine_reflection": refine_reflection_node,
        "fallback_report": fallback_report_node,
        "finalize_report": finalize_node,
    }
    for name, node in nodes.items():
        graph.add_node(name, _traced(name, node, routes))

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
    graph.add_conditional_edges(
        "generate_report",
        route_after_generation,
        {
            "validate": "validate_report",
            "finalize": "finalize_report",
            "fallback": "fallback_report",
        },
    )

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

    compiled = graph.compile()
    routes.update(route_labels(compiled))
    return compiled


def route_labels(graph: CompiledStateGraph[Any, Any, Any, Any]) -> dict[tuple[str, str], str]:
    """Map each conditional (source, target) node pair to its router key, from get_graph().

    Raises ValueError if a pair has two keys: the trace derives a route from the pair alone
    (ADR-008), so every pair must name exactly one. get_graph() draws one edge per pair and
    keeps only the first key, so the check reads every key from the builder's path maps.
    """
    keys: dict[tuple[str, str], set[str]] = {}
    for source, branches in graph.builder.branches.items():
        for branch in branches.values():
            for key, target in (branch.ends or {}).items():
                keys.setdefault((source, target), set()).add(str(key))
    ambiguous = {pair: sorted(found) for pair, found in keys.items() if len(found) > 1}
    if ambiguous:
        raise ValueError(f"Node pairs with more than one route key are ambiguous: {ambiguous}")

    labels: dict[tuple[str, str], str] = {}
    for edge in graph.get_graph().edges:
        if edge.conditional:
            # get_graph() leaves the label out when the router key is the target's own name.
            labels[(edge.source, edge.target)] = (
                str(edge.data) if edge.data is not None else edge.target
            )
    return labels


def _traced(
    name: str, node: Callable[..., Any], routes: Mapping[tuple[str, str], str]
) -> _TracedNode:
    """Wrap a node to record route_decided (if a router chose it), node_started, node_finished.

    The node's result or exception passes through unchanged (a cancelled node finishes with
    status "cancelled", a raising one with "error"); the wrapper only adds `previous_node`
    to the update, so the next node can derive the route from the pair. Routers stay pure.
    """
    takes_runtime = "runtime" in inspect.signature(node).parameters

    async def traced_node(state: TriageState, *, runtime: Runtime[TriageDeps]) -> dict[str, Any]:
        trace = runtime.context.trace
        previous = state.get("previous_node")
        route = routes.get((previous, name)) if previous is not None else None
        if previous is not None and route is not None:
            record_event(trace, lambda: RouteDecided(source=previous, target=name, route=route))
        record_event(trace, lambda: NodeStarted(node=name))
        started = time.monotonic()
        status: NodeStatus = "error"
        result: dict[str, Any] = {}
        try:
            result = node(state, runtime) if takes_runtime else node(state)
            if inspect.isawaitable(result):
                result = await result
            status = "ok"
            if name == "finalize_report":
                _record_reconciled(trace, state, result)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            record_event(
                trace,
                lambda: NodeFinished(
                    node=name,
                    duration_ms=duration_ms,
                    status=status,
                    confidence=_router_confidence(name, result),
                ),
            )
        return {**result, "previous_node": name}

    return traced_node


def _router_confidence(name: str, update: Mapping[str, Any]) -> float | None:
    """validate_report's output report confidence: what route_after_validation reads."""
    report = update.get("final_report") if name == "validate_report" else None
    return report.confidence_score if report is not None else None


def _record_reconciled(trace: TraceRecorder, state: TriageState, update: Mapping[str, Any]) -> None:
    """Record what finalize_report's reconcile() changed, if it had a report to reconcile.

    finalize_report stays a pure function of state, so its caller (this wrapper) records
    the event from the node's input and output reports.
    """
    before, after = state.get("final_report"), update.get("final_report")
    if before is not None and after is not None:
        record_event(trace, lambda: Reconciled.between(before, after))


triage_graph = build_triage_graph()
