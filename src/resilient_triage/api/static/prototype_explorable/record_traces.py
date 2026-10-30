"""PROTOTYPE (issue #96), throwaway: record real execution traces for the 13 catalogue scenarios.

Each scenario runs through the real TriageGateway, graph, LLMGateway, TelemetryProbe,
breakers, reconciliation and in-memory cache, with a ScriptedAdapter and a canned Statuspage.
TraceRecorder does not exist yet (ADR 0008), so this file approximates it by wrapping instance
methods and reading astream_events; the event shapes follow ADR 0008 (no free text on the wire).

Run from the repo root:
    PYTHONPATH=src .venv/bin/python <this file>
Writes traces.json next to this file.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from resilient_triage.cache.embeddings import DeterministicEmbeddingProvider
from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.config import Settings, settings
from resilient_triage.graph.builder import triage_graph
from resilient_triage.llm import ScriptedAdapter
from resilient_triage.llm.failures import ProviderFailure, RateLimited, Unavailable
from resilient_triage.llm.gateway import Completion
from resilient_triage.llm.scripted import DEFAULT_REPORT_JSON
from resilient_triage.runtime import build_runtime
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.telemetry import ChaosConfig
from resilient_triage.triage.gateway import Analyzed, CacheHit, Shed

OUT = Path(__file__).with_name("traces.json")
NODES = {n for n in triage_graph.get_graph().nodes if not n.startswith("__")}

# Scripted reply delays: ScriptedAdapter answers instantly, which flattens the waterfall.
# A declared per-reply delay is one of the open questions this prototype raises.
REPLY_DELAY_S = 0.9
FAILURE_DELAY_S = 0.18
STATUSPAGE_DELAY_S = 0.21

DEGRADED_SUMMARY: dict[str, Any] = {
    "page": {"name": "GitHub", "url": "https://www.githubstatus.com"},
    "status": {"indicator": "major", "description": "Partial System Outage"},
    "components": [
        {"id": "c1", "name": "Actions", "status": "major_outage", "description": None},
        {"id": "c2", "name": "Webhooks", "status": "degraded_performance", "description": None},
    ],
    "incidents": [
        {"id": "i1", "name": "Delayed Actions jobs", "status": "investigating", "impact": "major"},
    ],
}


def report(**changes: Any) -> str:
    data = json.loads(DEFAULT_REPORT_JSON)
    data.update(changes)
    return json.dumps(data)


INVALID = report(severity="CATASTROPHIC", confidence_score=1.7)
UNAVAILABLE = Unavailable(detail="HTTP 503")


@dataclass
class Scenario:
    id: str
    chapter: str
    title: str
    caption: str
    query: str
    scope: str | None = None
    script: list[str | ProviderFailure] = field(default_factory=list)
    statuspage_fails: bool = False
    sentinel_fails: bool = False
    sentinel_latency_ms: int = 0
    setup_failures: int = 0
    seed_query: str | None = None
    deadline_s: float | None = None
    shed: bool = False
    redis_down: bool = False
    setup_notes: list[str] = field(default_factory=list)


GH = "GitHub Actions jobs failing with 503 and delayed webhooks"
SCENARIOS = [
    Scenario(
        "healthy",
        "Fast paths",
        "Healthy run",
        "Every dependency up, one model call, a validated report.",
        GH,
    ),
    Scenario(
        "cache-hit",
        "Fast paths",
        "Cache hit",
        "A similar question was answered before, so the semantic cache answers.",
        GH,
        seed_query=GH + ".",
        setup_notes=["A similar question's report is already in this run's private cache."],
    ),
    Scenario(
        "failover",
        "Fast paths",
        "Model failover",
        "The primary model is rate-limited; the gateway fails over.",
        GH,
        script=[RateLimited(retry_after_s=30, scope="minute", detail="429"), report()],
    ),
    Scenario(
        "self-repair",
        "The model misbehaves",
        "Self-repair",
        "The reply breaks the contract and goes back to the model.",
        GH,
        script=[INVALID, report()],
    ),
    Scenario(
        "refine",
        "The model misbehaves",
        "Refine",
        "Low confidence triggers one reflection round.",
        GH,
        script=[report(confidence_score=0.45), report(confidence_score=0.82)],
    ),
    Scenario(
        "refine-provider-failure",
        "The model misbehaves",
        "Refine, then provider failure",
        "The second opinion never comes, so the first one stands.",
        GH,
        script=[report(confidence_score=0.45), UNAVAILABLE, UNAVAILABLE, UNAVAILABLE],
    ),
    Scenario(
        "repair-exhausted",
        "The model misbehaves",
        "Self-repair exhausted",
        "Out of retries, the report is built deterministically.",
        GH,
        script=[INVALID, INVALID, INVALID],
    ),
    Scenario(
        "reconciliation",
        "The model misbehaves",
        "Reconciliation",
        "reconcile overrules the model and requires approval.",
        GH,
        script=[
            report(
                recommended_actions=[
                    {
                        "priority": "P0",
                        "action": "Restart the Actions webhook consumer",
                        "target_system": "Actions Controller",
                        "action_type": "remediative",
                        "requires_approval": False,
                    }
                ]
            )
        ],
    ),
    Scenario(
        "unavailable-source",
        "Dependencies fail",
        "Unavailable source",
        "One failed read: degraded, but the run stays on the normal path.",
        GH,
        statuspage_fails=True,
    ),
    Scenario(
        "tripped-breaker",
        "Dependencies fail",
        "Tripped breaker",
        "The breaker was open before the run, so the agent compensates for the blind spot.",
        GH,
        statuspage_fails=True,
        setup_failures=3,
        setup_notes=["Statuspage failed 3 times before this run; its breaker is open."],
    ),
    Scenario(
        "run-deadline",
        "Dependencies fail",
        "Run deadline",
        "The run hits its deadline and is answered anyway.",
        "Internal auth service token validation latency spiking",
        sentinel_latency_ms=5000,
        deadline_s=3.0,
        setup_notes=["This scenario's run deadline is 3 s.", "Cluster Sentinel answers after 5 s."],
    ),
    Scenario(
        "shed",
        "Dependencies fail",
        "Shed request",
        "No capacity: refused promptly, not left hanging.",
        GH,
        shed=True,
        setup_notes=["All 2 run slots are already held.", "Queue wait is 1.5 s."],
    ),
    Scenario(
        "everything-down",
        "Finale",
        "Everything down",
        "Everything down, still a report, no 500.",
        "GitHub Actions webhooks and internal auth both failing",
        script=[UNAVAILABLE] * 6,
        statuspage_fails=True,
        sentinel_fails=True,
        setup_failures=3,
        redis_down=True,
        setup_notes=[
            "Redis refuses connections.",
            "Statuspage and Cluster Sentinel each failed 3 times before this run.",
            "Both models are failing.",
        ],
    ),
]


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.t0 = time.monotonic()
        self.phase = "run"

    def record(self, kind: str, **fields: Any) -> None:
        self.events.append({"seq": len(self.events), "t_ms": self.ms(), "kind": kind, **fields})

    def ms(self) -> float:
        return round((time.monotonic() - self.t0) * 1000, 1)


def failure_kind(result: Completion | ProviderFailure) -> str:
    return "completion" if isinstance(result, Completion) else type(result).__name__


def statuspage_http(sc: Scenario) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(STATUSPAGE_DELAY_S)
        if sc.statuspage_fails:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=DEGRADED_SUMMARY)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def record(sc: Scenario) -> dict[str, Any]:
    rec = Recorder()
    app_settings = Settings(
        triage_run_deadline_s=sc.deadline_s or settings.triage_run_deadline_s,
        triage_max_concurrent_runs=2 if sc.shed else settings.triage_max_concurrent_runs,
        triage_queue_timeout_s=1.5 if sc.shed else settings.triage_queue_timeout_s,
        redis_url="redis://127.0.0.1:1/0",
    )
    adapter = ScriptedAdapter(sc.script)
    rt = build_runtime(
        app_settings,
        llm_adapter=adapter,
        embedder=DeterministicEmbeddingProvider(),
        statuspage_http=statuspage_http(sc),
    )
    rt.cache = SemanticCacheManager(
        redis_url=app_settings.redis_url, embedding_provider=DeterministicEmbeddingProvider()
    )
    gw = rt.triage_gateway
    gw._cache = rt.cache
    rt.deps = rt.deps.__class__(llm=rt.deps.llm, telemetry=rt.deps.telemetry, history=rt.cache)
    await rt.cache.initialize()
    if sc.sentinel_fails or sc.sentinel_latency_ms:
        rt.chaos.update_config(
            ChaosConfig(
                active=True, inject_503=sc.sentinel_fails, latency_ms=sc.sentinel_latency_ms
            )
        )

    # --- model_call: one per adapter call, with model role and outcome
    llm = rt.llm
    real_call = adapter.call

    async def call(model: str, messages: Any) -> Completion | ProviderFailure:
        started = rec.ms()
        entry_index = len(adapter.calls)
        entry = sc.script[entry_index] if entry_index < len(sc.script) else None
        await asyncio.sleep(
            FAILURE_DELAY_S if entry is not None and not isinstance(entry, str) else REPLY_DELAY_S
        )
        result = await real_call(model, messages)
        rec.record(
            "model_call",
            model=model,
            role="primary" if model == llm.primary else "fallback",
            outcome=failure_kind(result),
            started_ms=started,
            duration_ms=round(rec.ms() - started, 1),
            cooldown=bool(isinstance(result, RateLimited)),
            scripted=True,
        )
        return result

    adapter.call = call  # type: ignore[method-assign]

    # --- telemetry_read: one per source read, with breaker state after
    probe = rt.deps.telemetry
    real_read = probe._read

    async def read(source: Any, query: str, scope: str | None) -> Any:
        started = rec.ms()
        before = probe._source_breakers[source.id].info().state
        outcome = await real_read(source, query, scope)
        after = probe._source_breakers[source.id].info().state
        rec.record(
            "telemetry_read",
            phase=rec.phase,
            source=source.id,
            outcome={"_Read": "read", "_Tripped": "tripped", "_Unavailable": "unavailable"}[
                type(outcome).__name__
            ],
            breaker_before=before,
            breaker_after=after,
            started_ms=started,
            duration_ms=round(rec.ms() - started, 1),
            signals=len(outcome.signals) if hasattr(outcome, "signals") else 1,
        )
        return outcome

    probe._read = read  # type: ignore[method-assign]

    # --- scenario setup: seed, pre-run failures, held slots
    if sc.seed_query:
        seed = IncidentTriageReport.model_validate_json(report())
        await rt.cache.store(query=sc.seed_query, report=seed, service_filter=sc.scope)
        rec.record("scenario_setup", step="seed_cache", entries=1)
    if sc.setup_failures:
        rec.phase = "setup"
        targets = probe.select_targets(sc.query, sc.scope)
        for _ in range(sc.setup_failures):
            await probe.collect(targets, sc.query, sc.scope)
        rec.phase = "run"
    held = 0
    if sc.shed:
        for _ in range(app_settings.triage_max_concurrent_runs):
            await gw._run_slots.acquire()
            held += 1
        rec.record("scenario_setup", step="hold_run_slots", held=held)

    rec.record("trace_started", schema_version=1, scenario_id=sc.id, scoped=sc.scope is not None)

    # --- cache_lookup / cache_store
    cache = rt.cache
    real_lookup, real_store = cache.lookup, cache.store

    async def lookup(**kw: Any) -> Any:
        started = rec.ms()
        match = await real_lookup(**kw)
        rec.record(
            "cache_lookup",
            backend=cache.driver,
            private=True,
            redis_refused=sc.redis_down,
            hit=match is not None,
            similarity=round(match.similarity, 3) if match else None,
            threshold=cache.similarity_threshold,
            entries=cache.count,
            started_ms=started,
            duration_ms=round(rec.ms() - started, 1),
        )
        return match

    async def store(**kw: Any) -> Any:
        started = rec.ms()
        result = await real_store(**kw)
        rec.record(
            "cache_store",
            backend=cache.driver,
            private=True,
            stored=True,
            started_ms=started,
            duration_ms=round(rec.ms() - started, 1),
        )
        return result

    cache.lookup, cache.store = lookup, store  # type: ignore[method-assign]

    # --- run_slot
    slots = gw._run_slots
    real_acquire = slots.acquire

    async def acquire() -> bool:
        started = rec.ms()
        try:
            got = await real_acquire()
        except BaseException:
            rec.record(
                "run_slot",
                acquired=False,
                waited_ms=round(rec.ms() - started, 1),
                capacity=app_settings.triage_max_concurrent_runs,
                in_flight=held,
            )
            raise
        rec.record(
            "run_slot",
            acquired=True,
            waited_ms=round(rec.ms() - started, 1),
            capacity=app_settings.triage_max_concurrent_runs,
            in_flight=held,
        )
        return got

    slots.acquire = acquire  # type: ignore[method-assign]

    # --- nodes via astream_events, plus validation_failed and reconciled
    async def run_graph(run_input: Any) -> dict[str, Any]:
        final: dict[str, Any] = {}
        open_nodes: dict[str, tuple[str, float, dict[str, Any]]] = {}
        try:
            async for ev in triage_graph.astream_events(run_input, context=rt.deps, version="v2"):
                name, meta = ev["name"], ev.get("metadata", {})
                if ev["event"] == "on_chain_end" and name == "LangGraph":
                    final = ev["data"]["output"]
                if name not in NODES or meta.get("langgraph_node") != name:
                    continue
                if ev["event"] == "on_chain_start" and ev["run_id"] not in open_nodes:
                    open_nodes[ev["run_id"]] = (name, rec.ms(), ev["data"].get("input") or {})
                    rec.record("node_started", node=name, step=meta.get("langgraph_step"))
                elif ev["event"] == "on_chain_end" and ev["run_id"] in open_nodes:
                    node, started, node_in = open_nodes.pop(ev["run_id"])
                    out = ev["data"].get("output") or {}
                    if node == "validate_report" and out.get("validation_errors"):
                        fields = sorted(
                            {
                                e.split("'")[1]
                                for e in out["validation_errors"]
                                if e.startswith("Field '")
                            }
                        )
                        rec.record(
                            "validation_failed",
                            attempt=out.get("repair_attempts"),
                            errors=len(out["validation_errors"]),
                            fields=fields,
                        )
                    if node == "finalize_report" and out.get("final_report"):
                        before, after = node_in.get("final_report"), out["final_report"]
                        changes = reconcile_changes(before, after)
                        rec.record("reconciled", changes=changes)
                    extra: dict[str, Any] = {}
                    if node == "validate_report" and out.get("final_report") is not None:
                        # Proposed optional field: the routers' confidence input, a number.
                        extra["confidence"] = out["final_report"].confidence_score
                    rec.record(
                        "node_finished",
                        node=node,
                        started_ms=started,
                        duration_ms=round(rec.ms() - started, 1),
                        **extra,
                    )
        except asyncio.CancelledError:
            for node, started, _ in open_nodes.values():
                rec.record(
                    "node_finished",
                    node=node,
                    started_ms=started,
                    duration_ms=round(rec.ms() - started, 1),
                    cancelled=True,
                )
            rec.record("deadline_exceeded", deadline_s=app_settings.triage_run_deadline_s)
            raise
        return final

    gw._run_graph = run_graph

    outcome = await gw.triage(sc.query, sc.scope)
    out: dict[str, Any] = {"outcome": type(outcome).__name__}
    if isinstance(outcome, CacheHit | Analyzed):
        out["report"] = json.loads(outcome.report.model_dump_json())
    if isinstance(outcome, CacheHit):
        out["similarity"] = round(outcome.similarity, 3)
    if isinstance(outcome, Shed):
        out["retry_after_s"] = 5
    rec.record(
        "trace_finished",
        triage_outcome={"CacheHit": "cache_hit", "Analyzed": "analyzed", "Shed": "shed"}[
            out["outcome"]
        ],
        response=out,
    )
    add_routes(rec.events)
    await rt.aclose()
    return {
        "id": sc.id,
        "chapter": sc.chapter,
        "title": sc.title,
        "caption": sc.caption,
        "query": sc.query,
        "setup": sc.setup_notes,
        "events": rec.events,
    }


def reconcile_changes(before: Any, after: Any) -> list[dict[str, Any]]:
    if before is None:
        return []
    b = before if isinstance(before, dict) else json.loads(before.model_dump_json())
    a = after if isinstance(after, dict) else json.loads(after.model_dump_json())
    changes: list[dict[str, Any]] = []
    if b["degradation_status"] != a["degradation_status"]:
        changes.append(
            {
                "field": "degradation_status",
                "from": b["degradation_status"],
                "to": a["degradation_status"],
            }
        )
    if b["circuit_breakers_tripped"] != a["circuit_breakers_tripped"]:
        changes.append(
            {
                "field": "circuit_breakers_tripped",
                "from": b["circuit_breakers_tripped"],
                "to": a["circuit_breakers_tripped"],
            }
        )
    for i, (x, y) in enumerate(
        zip(b["recommended_actions"], a["recommended_actions"], strict=True)
    ):
        if x["requires_approval"] != y["requires_approval"]:
            changes.append(
                {
                    "field": f"recommended_actions[{i}].requires_approval",
                    "from": x["requires_approval"],
                    "to": y["requires_approval"],
                }
            )
    return changes


def add_routes(events: list[dict[str, Any]]) -> None:
    """Derive route_decided from consecutive node pairs and get_graph() edge labels (ADR 0008)."""
    labels = {(e.source, e.target): e.data for e in triage_graph.get_graph().edges if e.data}
    starts = [e for e in events if e["kind"] == "node_started"]
    inserts: list[tuple[int, dict[str, Any]]] = []
    for prev, nxt in zip(starts, starts[1:], strict=False):
        key = labels.get((prev["node"], nxt["node"]))
        if key:
            inserts.append(
                (
                    nxt["seq"],
                    {
                        "kind": "route_decided",
                        "t_ms": nxt["t_ms"],
                        "from": prev["node"],
                        "to": nxt["node"],
                        "route": key,
                    },
                )
            )
    for seq, ev in sorted(inserts, key=lambda p: p[0], reverse=True):
        events.insert(seq, ev)
    for i, ev in enumerate(events):
        ev["seq"] = i


async def main() -> None:
    traces = [await record(sc) for sc in SCENARIOS]
    graph = triage_graph.get_graph()
    OUT.write_text(
        json.dumps(
            {
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "graph": {
                    "nodes": sorted(NODES),
                    "edges": [
                        {
                            "from": e.source,
                            "to": e.target,
                            "label": e.data,
                            "conditional": e.conditional,
                        }
                        for e in graph.edges
                    ],
                },
                "models": {
                    "primary": settings.groq_model,
                    "fallback": settings.groq_fallback_model,
                },
                "scenarios": traces,
            },
            indent=1,
        )
    )
    for t in traces:
        path = [e["node"] for e in t["events"] if e["kind"] == "node_started"]
        print(
            f"{t['id']:26} {t['events'][-1]['triage_outcome']:10} "
            f"{t['events'][-1]['t_ms']:>8.0f}ms  {' > '.join(path)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
