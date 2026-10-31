"""The graph run's execution trace (ADR-008): node timing, derived routes, calls and reads.

Sequences are compared on (kind, key fields) only: no timing assertions.
"""

import asyncio
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fakes import make_triage_deps
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from resilient_triage.graph.builder import build_triage_graph, route_labels
from resilient_triage.graph.deps import TriageDeps
from resilient_triage.llm import (
    DEFAULT_REPORT_JSON,
    LLMGateway,
    RateLimited,
    ScriptedAdapter,
    Unavailable,
)
from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry, force_open_for_tests
from resilient_triage.schemas.incident import TelemetrySignal
from resilient_triage.schemas.state import TriageState
from resilient_triage.telemetry.probe import TelemetryProbe
from resilient_triage.trace.events import ModelCall, TelemetryRead
from resilient_triage.trace.recorder import ListTrace

MESSAGES: list[BaseMessage] = [HumanMessage(content="Is checkout down?")]

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


def test_every_conditional_edge_maps_its_node_pair_to_one_route_label() -> None:
    graph = build_triage_graph()

    labels = route_labels(graph)

    assert labels == {
        ("collect_telemetry", "compensate_blindspots"): "compensate",
        ("collect_telemetry", "synthesize_telemetry"): "synthesize",
        ("generate_report", "validate_report"): "validate",
        ("generate_report", "finalize_report"): "finalize",
        ("generate_report", "fallback_report"): "fallback",
        ("validate_report", "finalize_report"): "finalize",
        ("validate_report", "self_repair"): "repair",
        ("validate_report", "refine_reflection"): "refine",
        ("validate_report", "fallback_report"): "fallback",
    }
    conditional = {(e.source, e.target) for e in graph.get_graph().edges if e.conditional}
    assert conditional == set(labels)


def test_a_node_pair_reached_by_two_router_keys_is_ambiguous() -> None:
    graph = StateGraph(TriageState)
    graph.add_node("a", lambda state: {})
    graph.add_node("b", lambda state: {})
    graph.add_edge(START, "a")
    graph.add_conditional_edges("a", lambda state: "left", {"left": "b", "right": "b"})
    graph.add_edge("b", END)

    with pytest.raises(ValueError, match="ambiguous"):
        route_labels(graph.compile())


class _ExplodingLLM:
    async def complete(self, messages: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")


class _HangingLLM:
    async def complete(self, messages: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)


async def test_a_node_that_raises_finishes_with_status_error_and_the_error_propagates() -> None:
    deps = replace(make_triage_deps(), llm=_ExplodingLLM())
    trace = ListTrace()

    with pytest.raises(RuntimeError, match="boom"):
        await build_triage_graph().ainvoke(
            {"query": HEALTHY_QUERY}, context=replace(deps, trace=trace)
        )

    assert _key(trace.events[-1]) == ("node_finished", "generate_report", "error")


async def test_a_cancelled_node_finishes_with_status_cancelled() -> None:
    deps = replace(make_triage_deps(), llm=_HangingLLM())
    trace = ListTrace()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await build_triage_graph().ainvoke(
                {"query": HEALTHY_QUERY}, context=replace(deps, trace=trace)
            )

    assert _key(trace.events[-1]) == ("node_finished", "generate_report", "cancelled")


class _RaisingTrace:
    """Breaks the recorder contract: every record() raises."""

    def __init__(self) -> None:
        self.calls = 0

    def record(self, event: Any) -> None:
        self.calls += 1
        raise RuntimeError("recorder broke")


def _comparable(state: dict[str, Any]) -> dict[str, Any]:
    """The run's result without the per-run id, timestamp and measured latencies."""
    report = state["final_report"].model_dump(exclude={"incident_id", "timestamp"})
    for signal in report["telemetry_signals"]:
        signal.pop("latency_ms")
    return {"report": report, "repair_attempts": state.get("repair_attempts")}


@pytest.mark.parametrize(
    "script",
    [[], ["not json", DEFAULT_REPORT_JSON], [Unavailable(detail="down")] * 3],
    ids=["healthy", "self_repair", "provider_failure"],
)
async def test_a_recorder_that_raises_does_not_change_the_run_result(script: list[Any]) -> None:
    untraced = await build_triage_graph().ainvoke(
        {"query": HEALTHY_QUERY}, context=make_triage_deps(llm_adapter=ScriptedAdapter(script))
    )
    raising = _RaisingTrace()
    deps = make_triage_deps(llm_adapter=ScriptedAdapter(script))

    traced = await build_triage_graph().ainvoke(
        {"query": HEALTHY_QUERY}, context=replace(deps, trace=raising)
    )

    assert raising.calls > 0
    assert _comparable(traced) == _comparable(untraced)


def _llm(script: list[Any]) -> LLMGateway:
    return LLMGateway(
        ScriptedAdapter(script), primary="primary-m", fallback="fallback-m", label="t"
    )


async def test_each_adapter_attempt_is_one_model_call() -> None:
    gateway = _llm([Unavailable(detail="503"), Unavailable(detail="503"), DEFAULT_REPORT_JSON])
    trace = ListTrace()

    await gateway.complete(MESSAGES, trace=trace)

    calls = [(e.model, e.role, e.outcome) for e in trace.events if isinstance(e, ModelCall)]
    assert calls == [
        ("primary-m", "primary", "unavailable"),
        ("primary-m", "primary", "unavailable"),
        ("fallback-m", "fallback", "completion"),
    ]


async def test_a_rate_limited_model_call_keeps_its_scope_and_retry_after() -> None:
    gateway = _llm([RateLimited(retry_after_s=7.0, scope="minute", detail="429")])
    trace = ListTrace()

    await gateway.complete(MESSAGES, trace=trace)
    await gateway.complete(MESSAGES, trace=trace)  # the primary cools down: fallback only

    calls = [
        (e.role, e.outcome, e.rate_limit_scope, e.retry_after_s)
        for e in trace.events
        if isinstance(e, ModelCall)
    ]
    assert calls == [
        ("primary", "rate_limited", "minute", 7.0),
        ("fallback", "completion", None, None),
        ("fallback", "completion", None, None),
    ]


async def test_an_adapter_that_raises_is_one_unavailable_model_call() -> None:
    class Exploding(ScriptedAdapter):
        async def call(self, model: str, messages: Any) -> Any:
            raise RuntimeError("socket gone")

    gateway = LLMGateway(Exploding(), primary="primary-m", fallback="fallback-m", label="t")
    trace = ListTrace()

    result = await gateway.complete(MESSAGES, trace=trace)

    assert isinstance(result, Unavailable)
    assert [(e.kind, e.outcome) for e in trace.events] == [("model_call", "unavailable")]


class _Source:
    """A Telemetry source that raises each queued error in turn, then returns one signal."""

    def __init__(self, source_id: str, *errors: Exception) -> None:
        self.id = source_id
        self.keywords: frozenset[str] = frozenset()
        self.display_name = source_id
        self.errors = list(errors)

    async def read(self, query: str, scope: str | None) -> list[TelemetrySignal]:
        if self.errors:
            raise self.errors.pop(0)
        return [TelemetrySignal(source=f"{self.id}:ok", status="OK")]

    def degraded_label(self, query: str, scope: str | None) -> str:
        return self.id


def _reads(trace: ListTrace) -> list[tuple[Any, ...]]:
    return [
        (e.source, e.phase, e.outcome, e.attempts, e.breaker_before, e.breaker_after, e.signals)
        for e in trace.events
        if isinstance(e, TelemetryRead)
    ]


async def test_each_source_read_is_one_telemetry_read_with_its_attempts() -> None:
    timeout = httpx.ConnectTimeout("slow")
    probe = TelemetryProbe(
        [_Source("alpha"), _Source("beta", timeout), _Source("gamma", RuntimeError("down"))],
        breakers=CircuitBreakerRegistry(),
    )
    trace = ListTrace()

    await probe.collect(frozenset({"alpha", "beta", "gamma"}), "q", None, trace=trace)

    assert sorted(_reads(trace)) == [
        ("alpha", None, "read", 1, "closed", "closed", 1),
        ("beta", None, "read", 2, "closed", "closed", 1),
        ("gamma", None, "unavailable", 1, "closed", "closed", 1),
    ]


async def test_a_read_records_its_breaker_state_before_and_after() -> None:
    breakers = CircuitBreakerRegistry()
    probe = TelemetryProbe(
        [_Source("alpha", RuntimeError("down")), _Source("beta")], breakers=breakers, fail_max=1
    )
    force_open_for_tests(breakers.get_or_create("beta"))
    trace = ListTrace()

    await probe.collect(frozenset({"alpha", "beta"}), "q", None, trace=trace, phase="setup")

    assert sorted(_reads(trace)) == [
        ("alpha", "setup", "tripped", 1, "closed", "open", 1),
        ("beta", "setup", "tripped", 0, "open", "open", 1),
    ]


async def test_the_shared_deps_keep_their_null_trace() -> None:
    deps = make_triage_deps()
    shared_trace = deps.trace

    await _traced_run(deps)

    assert deps.trace is shared_trace
    assert type(shared_trace).__name__ == "NullTrace"
