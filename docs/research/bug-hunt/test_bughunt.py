"""Bug-hunt repros. Each test asserts the CORRECT behaviour; a failure confirms the defect."""

import asyncio
import json
import time

import httpx
import numpy as np
import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from resilient_triage.cache.semantic_cache import semantic_cache
from resilient_triage.config import settings
from resilient_triage.graph import llm as llm_mod
from resilient_triage.graph.builder import build_triage_graph
from resilient_triage.graph.llm import (
    GroqChatModel,
    MockTriageChatModel,
    get_active_model_name,
    get_triage_llm,
    groq_quota_tracker,
    set_triage_llm,
)
from resilient_triage.resilience.circuit_breaker import circuit_breaker_registry
from resilient_triage.schemas.incident import DegradationStatus
from resilient_triage.schemas.telemetry import ChaosConfig
from resilient_triage.telemetry.chaos import ChaosClient, chaos_manager

# Internal-only keywords => only the in-process chaos probe is targeted (no real network).
Q = "internal database connection pool exhausted on payment workers"


def _report(conf: float) -> str:
    return json.dumps(
        {
            "summary": "Payment workers exhausting DB pool",
            "severity": "HIGH",
            "root_cause_analysis": "Connection leak in payment worker release path",
            "recommended_actions": [],
            "confidence_score": conf,
        }
    )


class FailAfterFirst(MockTriageChatModel):
    """Returns responses[0], then raises a provider error on every later call."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.call_count >= 1:
            self.call_count += 1
            raise httpx.ConnectError("provider down")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class AlwaysFail(MockTriageChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise httpx.ConnectError("provider down")


def _force_in_memory(monkeypatch):
    async def _noop(*_a, **_k):
        return None

    semantic_cache._redis = None
    semantic_cache._has_redisearch = False
    semantic_cache._initialized = True
    monkeypatch.setattr(semantic_cache, "_ensure_connected", _noop)


# --- R1: refinement throws away a valid report ------------------------------------------
async def test_r1_refine_failure_keeps_valid_report():
    set_triage_llm(FailAfterFirst(responses=[_report(0.5)]))
    graph = build_triage_graph(enable_checkpointing=False)
    out = await graph.ainvoke({"query": Q})
    rep = out["final_report"]
    assert rep.confidence_score == 0.5, (rep.confidence_score, rep.degradation_status)


# --- R2: fallback report is recycled as a "previous incident" root cause ------------------
async def test_r2_fallback_report_not_used_as_history(monkeypatch):
    _force_in_memory(monkeypatch)
    graph = build_triage_graph(enable_checkpointing=False)
    set_triage_llm(AlwaysFail())
    first = await graph.ainvoke({"query": Q})
    assert first["final_report"].confidence_score == 0.20  # fallback was produced and stored

    circuit_breaker_registry.get_or_create("chaos").breaker.open()
    set_triage_llm(MockTriageChatModel())
    second = await graph.ainvoke({"query": Q + " again"})
    history = " ".join(second["historical_context"])
    assert "Self-repair limits reached" not in history, history


async def test_r2b_fallback_report_served_as_cache_hit(client):
    set_triage_llm(AlwaysFail())
    r1 = await client.post("/triage", json={"query": Q})
    set_triage_llm(MockTriageChatModel())  # provider recovered
    r2 = await client.post("/triage", json={"query": Q})
    assert r2.headers["X-Cache"] == "MISS", (
        r2.headers["X-Cache"],
        r2.json()["report"]["confidence_score"],
        r1.json()["report"]["root_cause_analysis"][:60],
    )


# --- R3: cancellation counts as a breaker failure ----------------------------------------
async def test_r3_cancellation_does_not_trip_breaker():
    cb = circuit_breaker_registry.get_or_create("bh_cancel", fail_max=1)

    async def slow():
        await asyncio.sleep(10)

    task = asyncio.create_task(cb.call(slow))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(BaseException):
        await task
    assert cb.current_state == "closed", (cb.current_state, cb.fail_counter)


# --- R4: retry re-sends to the rate-limited primary --------------------------------------
async def test_r4_429_not_retried_against_primary(monkeypatch):
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content)["model"])
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    monkeypatch.setattr(
        llm_mod, "_groq_async_client", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    model = GroqChatModel(api_key="test", model_name="primary")
    with pytest.raises(httpx.HTTPStatusError):
        await model.ainvoke([HumanMessage(content="hi")])
    assert calls.count("primary") == 1, calls


# --- R5: fallback model's 429 sets the primary's cooldown ---------------------------------
def test_r5_fallback_429_does_not_extend_primary_cooldown():
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(429, request=req, json={"error": {"message": "fb limited"}})
    groq_quota_tracker.record_response(resp, model_name="llama-3.1-8b-instant", is_failover=True)
    assert not groq_quota_tracker.is_primary_rate_limited()


# --- R6: chaos latency is unbounded and the probe has no deadline -------------------------
def test_r6a_chaos_latency_is_bounded():
    with pytest.raises(ValidationError):
        ChaosConfig(active=True, latency_ms=86_400_000)  # 24h


async def test_r6b_chaos_probe_has_deadline():
    chaos_manager.update_config(ChaosConfig(active=True, latency_ms=4000))
    start = time.monotonic()
    await asyncio.wait_for(ChaosClient().fetch_signals(), timeout=10)
    assert time.monotonic() - start < 3.5  # Statuspage's read timeout is 3s


# --- R7: a bad in-memory cache entry turns every similar request into a 500 ---------------
async def test_r7a_corrupt_cache_entry_does_not_500(client, monkeypatch):
    _force_in_memory(monkeypatch)
    await client.post("/triage", json={"query": Q})
    for entry in semantic_cache._in_memory_docs.values():
        entry.report_json = '{"summary": "x"}'
    r = await client.post("/triage", json={"query": Q})
    assert r.status_code == 200, r.text


async def test_r7b_mismatched_vector_dim_does_not_500(client, monkeypatch):
    _force_in_memory(monkeypatch)
    await client.post("/triage", json={"query": Q})
    next(iter(semantic_cache._in_memory_docs.values())).vector = np.zeros(768, np.float32)
    await semantic_cache.store("other query", (await _any_report()), None)
    r = await client.post("/triage", json={"query": Q})
    assert r.status_code == 200, r.text


async def _any_report():
    from resilient_triage.graph.nodes.fallback_report import fallback_report_node

    return fallback_report_node({"query": "x"})["final_report"]


# --- R8: unscoped lookups match scoped entries -------------------------------------------
async def test_r8_unscoped_query_does_not_hit_scoped_entry(monkeypatch):
    _force_in_memory(monkeypatch)
    await semantic_cache.store(Q, await _any_report(), service_filter="billing")
    match = await semantic_cache.lookup(Q, service_filter=None)
    assert match is None, (match.similarity, match.matched_query)


# --- R9: embedding failure is a 500 -------------------------------------------------------
async def test_r9_embedding_failure_does_not_500(client, monkeypatch):
    def boom(_t):
        raise RuntimeError("onnxruntime: bad alloc")

    monkeypatch.setattr(semantic_cache.embedding_provider, "embed_query", boom)
    r = await client.post("/triage", json={"query": Q})
    assert r.status_code == 200, r.text


# --- R10: recursion limit at the max repair setting (expected to PASS) --------------------
async def test_r10_max_repairs_with_refine_within_recursion_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_self_repair_retries", 5)
    circuit_breaker_registry.get_or_create("chaos").breaker.open()  # longest prefix path
    set_triage_llm(MockTriageChatModel(responses=[_report(0.5)] + ["not json"] * 10))
    graph = build_triage_graph(enable_checkpointing=False)
    out = await graph.ainvoke({"query": Q})
    assert out["final_report"].degradation_status != DegradationStatus.HEALTHY


# --- R11: /health names a model the factory will not build --------------------------------
def test_r11_active_model_label_matches_factory(monkeypatch):
    set_triage_llm(None)
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    label, built = get_active_model_name(), type(get_triage_llm()).__name__
    assert not (label == "OpenAI GPT-4o" and built == "MockTriageChatModel"), (label, built)


# --- R12: /cache/clear while Redis is disconnected leaves Redis entries behind -------------
async def test_r12_clear_while_disconnected_clears_redis():
    await semantic_cache.initialize()
    assert semantic_cache.is_redis_connected, "needs a local Redis"
    await semantic_cache.store(Q, await _any_report(), None)
    semantic_cache._redis = None  # as after a failed ping / store error
    await semantic_cache.clear()  # what POST /cache/clear calls; returns "ok"
    semantic_cache._last_reconnect_attempt = 0.0
    match = await semantic_cache.lookup(Q)  # reconnects
    assert match is None, (semantic_cache.driver, match.similarity)
