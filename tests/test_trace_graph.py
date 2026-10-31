"""The graph run's execution trace (ADR-008): node timing, derived routes, calls and reads.

Sequences are compared on (kind, key fields) only: no timing assertions.
"""

from dataclasses import replace
from typing import Any

from fakes import make_triage_deps

from resilient_triage.graph.builder import build_triage_graph
from resilient_triage.graph.deps import TriageDeps
from resilient_triage.llm import ScriptedAdapter
from resilient_triage.trace.recorder import ListTrace

HEALTHY_QUERY = "GitHub Actions jobs are queued indefinitely"


def _key(event: Any) -> tuple[Any, ...]:
    """An event's kind and the fields a sequence test compares (never timings)."""
    match event.kind:
        case "node_started":
            return (event.kind, event.node)
        case "node_finished":
            return (event.kind, event.node, event.status)
        case "route_decided":
            return (event.kind, event.source, event.target, event.route)
        case "model_call":
            return (event.kind, event.role, event.outcome)
        case "telemetry_read":
            return (event.kind, event.source, event.outcome)
        case "validation_failed":
            return (event.kind, event.attempt)
        case _:
            return (event.kind,)


async def _traced_run(
    deps: TriageDeps, query: str = HEALTHY_QUERY, **inputs: Any
) -> tuple[list[tuple[Any, ...]], ListTrace, dict[str, Any]]:
    """Run the graph once with a ListTrace on a per-run copy of deps."""
    trace = ListTrace()
    state = await build_triage_graph().ainvoke(
        {"query": query, **inputs}, context=replace(deps, trace=trace)
    )
    return [_key(e) for e in trace.events], trace, state


def _nodes_and_routes(keys: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    kinds = {"node_started", "node_finished", "route_decided"}
    return [k for k in keys if k[0] in kinds]


def _node(name: str) -> list[tuple[Any, ...]]:
    return [("node_started", name), ("node_finished", name, "ok")]


async def test_healthy_run_records_every_node_and_each_conditional_route() -> None:
    keys, _, _ = await _traced_run(make_triage_deps(llm_adapter=ScriptedAdapter()))

    assert _nodes_and_routes(keys) == [
        *_node("analyze_query"),
        *_node("collect_telemetry"),
        ("route_decided", "collect_telemetry", "synthesize_telemetry", "synthesize"),
        *_node("synthesize_telemetry"),
        *_node("generate_report"),
        ("route_decided", "generate_report", "validate_report", "validate"),
        *_node("validate_report"),
        ("route_decided", "validate_report", "finalize_report", "finalize"),
        *_node("finalize_report"),
    ]


async def test_the_shared_deps_keep_their_null_trace() -> None:
    deps = make_triage_deps()
    shared_trace = deps.trace

    await _traced_run(deps)

    assert deps.trace is shared_trace
    assert type(shared_trace).__name__ == "NullTrace"
