"""Tests for the LLM gateway: provider selection, the gateway seam, and its adapters."""

import asyncio
import json
import sys
import types
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fakes import make_triage_deps
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.runtime import Runtime

from resilient_triage.config import settings
from resilient_triage.graph.nodes.generate_report import generate_report_node
from resilient_triage.llm import (
    DEFAULT_REPORT_JSON,
    Completion,
    GroqAdapter,
    LLMGateway,
    MalformedResponse,
    ModelAdapter,
    ProviderFailure,
    RateLimited,
    Rejected,
    ScriptedAdapter,
    Unavailable,
    build_llm_gateway,
)
from resilient_triage.runtime import build_runtime
from resilient_triage.schemas.incident import IncidentTriageReport, SeverityLevel

PRIMARY = "primary-model"
FALLBACK = "fallback-model"
MESSAGES: list[BaseMessage] = [HumanMessage(content="Is checkout down?")]


def _gateway(adapter: ModelAdapter) -> LLMGateway:
    return LLMGateway(adapter, primary=PRIMARY, fallback=FALLBACK, label="test")


def _groq(handler: Callable[[httpx.Request], httpx.Response]) -> GroqAdapter:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GroqAdapter("test-key", timeout_s=5.0, http_client=http)


def _content(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


async def test_openai_key_without_groq_key_runs_the_labelled_simulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (bug-hunt r11): only a Groq key selects a real provider; OpenAI is ignored."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # Even with langchain_openai importable, the OpenAI key must not pick a provider.
    stub = types.ModuleType("langchain_openai")
    stub.ChatOpenAI = lambda **kwargs: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_openai", stub)

    runtime = build_runtime(settings.model_copy(update={"groq_api_key": None}))
    try:
        snapshot = await runtime.snapshot()
        assert snapshot.active_model == "Zero-Config SRE Simulation"
        assert isinstance(runtime.llm.adapter, ScriptedAdapter)
    finally:
        await runtime.aclose()


async def test_scripted_adapter_returns_its_script_then_the_default_report() -> None:
    adapter = ScriptedAdapter(["first", Rejected(detail="bad request")])

    results = [await adapter.call(model, MESSAGES) for model in ("a", "b", "c")]

    assert results[0] == Completion(text="first", model="a")
    assert results[1] == Rejected(detail="bad request")
    assert results[2] == Completion(text=DEFAULT_REPORT_JSON, model="c")
    assert adapter.calls == ["a", "b", "c"]


async def test_default_report_is_a_valid_triage_report() -> None:
    report = IncidentTriageReport.model_validate_json(DEFAULT_REPORT_JSON)

    assert report.severity == SeverityLevel.HIGH


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(429, json={"error": {"message": "Rate limit"}}), RateLimited),
        (httpx.Response(503, text="upstream overloaded"), Unavailable),
        (httpx.Response(400, json={"error": {"message": "bad model"}}), Rejected),
        (httpx.Response(200, json={"choices": []}), MalformedResponse),
        (httpx.Response(200, text="not json"), MalformedResponse),
    ],
)
async def test_groq_adapter_maps_each_response_to_a_failure_kind(
    response: httpx.Response, kind: type
) -> None:
    adapter = _groq(lambda request: response)

    result = await adapter.call(PRIMARY, MESSAGES)

    assert isinstance(result, kind)


async def test_groq_adapter_maps_a_timeout_to_unavailable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    result = await _groq(timeout).call(PRIMARY, MESSAGES)

    assert isinstance(result, Unavailable)
    assert "timed out" in result.detail


async def test_groq_adapter_posts_one_json_mode_request_and_returns_its_content() -> None:
    sent: list[dict[str, Any]] = []

    def groq_api(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json=_content('{"summary": "ok"}'))

    result = await _groq(groq_api).call(PRIMARY, MESSAGES)

    assert result == Completion(text='{"summary": "ok"}', model=PRIMARY)
    assert len(sent) == 1
    assert sent[0]["model"] == PRIMARY
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert sent[0]["messages"] == [{"role": "user", "content": "Is checkout down?"}]


async def test_groq_adapter_reports_rate_limit_headers_to_the_gateway_snapshot() -> None:
    headers = {
        "x-ratelimit-remaining-requests": "41",
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-remaining-tokens": "500",
        "x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-reset-requests": "2m59.56s",
        "x-ratelimit-reset-tokens": "7.66s",
    }
    gateway = _gateway(_groq(lambda r: httpx.Response(200, json=_content("{}"), headers=headers)))

    await gateway.complete(MESSAGES)
    snapshot = gateway.snapshot()

    assert (snapshot.remaining_requests, snapshot.limit_requests) == (41, 1000)
    assert (snapshot.remaining_tokens, snapshot.limit_tokens) == (500, 8000)
    assert (snapshot.reset_requests, snapshot.reset_tokens) == ("2m59.56s", "7.66s")
    assert snapshot.last_status == 200
    assert snapshot.total_calls == 1
    assert snapshot.active_model == PRIMARY


async def test_a_groq_key_selects_groq_with_the_configured_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []

    def tracked_client(timeout: float) -> httpx.AsyncClient:
        timeouts.append(timeout)
        return httpx.AsyncClient()

    monkeypatch.setattr("resilient_triage.llm.groq.new_groq_http_client", tracked_client)
    groq_settings = settings.model_copy(
        update={"groq_api_key": "test-key", "groq_model": "m", "llm_request_timeout_s": 7.5}
    )

    gateway = build_llm_gateway(groq_settings)

    assert isinstance(gateway.adapter, GroqAdapter)
    assert gateway.label == "Groq LPU (m)"
    assert timeouts == [7.5]
    await gateway.aclose()


async def test_the_fallback_model_comes_from_settings() -> None:
    adapter = ScriptedAdapter([RateLimited(retry_after_s=None, scope="unknown", detail="429")])
    gateway = build_llm_gateway(
        settings.model_copy(update={"groq_fallback_model": "fb-model"}), adapter
    )

    await gateway.complete(MESSAGES)

    assert gateway.snapshot().fallback_model == "fb-model"
    assert adapter.calls[-1] == "fb-model"


async def test_complete_returns_unavailable_when_the_adapter_raises() -> None:
    class ExplodingAdapter(ScriptedAdapter):
        async def call(
            self, model: str, messages: list[BaseMessage]
        ) -> Completion | ProviderFailure:
            raise RuntimeError("adapter bug")

    result = await _gateway(ExplodingAdapter()).complete(MESSAGES)

    assert isinstance(result, Unavailable)
    assert "adapter bug" in result.detail


async def test_a_cancelled_call_propagates_and_records_nothing() -> None:
    class HangingAdapter(ScriptedAdapter):
        async def call(
            self, model: str, messages: list[BaseMessage]
        ) -> Completion | ProviderFailure:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    gateway = _gateway(HangingAdapter())
    task = asyncio.create_task(gateway.complete(MESSAGES))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = gateway.snapshot()
    assert (snapshot.total_calls, snapshot.rate_limit_hits, snapshot.failover_events) == (0, 0, 0)


async def test_a_rejected_call_is_returned_without_retry_or_failover() -> None:
    adapter = ScriptedAdapter([Rejected(detail="HTTP 400: bad model")])

    result = await _gateway(adapter).complete(MESSAGES)

    assert result == Rejected(detail="HTTP 400: bad model")
    assert adapter.calls == [PRIMARY]


async def test_reset_zeroes_counters_and_ends_the_cooldown() -> None:
    limited = RateLimited(retry_after_s=60.0, scope="unknown", detail="Rate limit")
    adapter = ScriptedAdapter([limited])
    gateway = _gateway(adapter)
    await gateway.complete(MESSAGES)
    assert adapter.calls == [PRIMARY, FALLBACK]

    gateway.reset()
    await gateway.complete(MESSAGES)

    assert adapter.calls[-1] == PRIMARY
    snapshot = gateway.snapshot()
    assert (snapshot.total_calls, snapshot.rate_limit_hits, snapshot.failover_events) == (1, 0, 0)
    assert snapshot.last_error is None


async def test_generate_report_turns_a_provider_failure_into_todays_state_shape() -> None:
    failure = Unavailable(detail="HTTP 503: upstream overloaded")

    result = await generate_report_node(
        {"messages": MESSAGES},
        Runtime(context=make_triage_deps(llm_adapter=ScriptedAdapter([failure] * 3))),
    )

    assert result["candidate_report_raw"] == ""
    assert result["validation_errors"] == [
        "LLM Provider Failure: Unavailable: HTTP 503: upstream overloaded"
    ]
    assert result["repair_attempts"] == settings.max_self_repair_retries


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_429_is_never_retried_against_the_primary() -> None:
    """Regression (bug-hunt r4): every call 429s, so exactly primary then fallback is sent."""
    limited = RateLimited(retry_after_s=None, scope="unknown", detail="Rate limit")
    adapter = ScriptedAdapter([limited] * 5)
    gateway = _gateway(adapter)

    result = await gateway.complete(MESSAGES)

    assert isinstance(result, RateLimited)
    assert adapter.calls == [PRIMARY, FALLBACK]
    assert gateway.snapshot().rate_limit_hits == 2


async def test_a_fallback_429_does_not_cool_down_the_primary() -> None:
    """Regression (bug-hunt r5): the fallback's 429 leaves the primary's window and budget."""
    sent: list[str] = []
    primary_budget = {"x-ratelimit-remaining-requests": "0", "x-ratelimit-limit-requests": "1000"}
    replies = {
        PRIMARY: [
            httpx.Response(
                429,
                json={"error": {"message": "primary limited"}},
                headers={**primary_budget, "x-ratelimit-reset-requests": "10s"},
            ),
            httpx.Response(200, json=_content("{}"), headers=primary_budget),
        ],
        FALLBACK: [
            httpx.Response(200, json=_content("{}")),
            httpx.Response(
                429,
                json={"error": {"message": "fallback limited"}},
                headers={
                    "x-ratelimit-remaining-requests": "0",
                    "x-ratelimit-limit-requests": "14400",
                    "x-ratelimit-reset-requests": "600s",
                },
            ),
        ],
    }

    def groq_api(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        sent.append(model)
        return replies[model].pop(0)

    clock = FakeClock()
    gateway = LLMGateway(
        _groq(groq_api), primary=PRIMARY, fallback=FALLBACK, label="test", clock=clock
    )
    await gateway.complete(MESSAGES)  # the primary's 429 starts its cooldown; the fallback answers
    clock.now += 5
    assert isinstance(await gateway.complete(MESSAGES), RateLimited)  # the fallback 429s
    assert gateway.snapshot().limit_requests == 1000

    clock.now += settings.llm_default_cooldown_s  # past the primary's own window
    result = await gateway.complete(MESSAGES)

    assert sent == [PRIMARY, FALLBACK, FALLBACK, PRIMARY]
    assert result == Completion(text="{}", model=PRIMARY)
    assert (gateway.snapshot().remaining_requests, gateway.snapshot().limit_requests) == (0, 1000)


LIMITED = RateLimited(retry_after_s=None, scope="unknown", detail="429")
DOWN = Unavailable(detail="HTTP 503")
GARBLED = MalformedResponse(detail="HTTP 200 without message content")


@pytest.mark.parametrize(
    ("script", "calls", "failovers"),
    [
        ([LIMITED, "from fallback"], [PRIMARY, FALLBACK], 1),
        ([DOWN, "from primary"], [PRIMARY, PRIMARY], 0),
        ([DOWN, DOWN, "from fallback"], [PRIMARY, PRIMARY, FALLBACK], 1),
        ([GARBLED, "from fallback"], [PRIMARY, FALLBACK], 1),
    ],
    ids=["rate-limited", "unavailable-once", "unavailable-twice", "malformed"],
)
async def test_the_primary_result_decides_retry_and_failover(
    script: list[str | ProviderFailure], calls: list[str], failovers: int
) -> None:
    adapter = ScriptedAdapter(script)
    gateway = _gateway(adapter)

    result = await gateway.complete(MESSAGES)

    assert isinstance(result, Completion)
    assert result.text == script[-1]
    assert adapter.calls == calls
    snapshot = gateway.snapshot()
    assert snapshot.active_model == calls[-1]
    assert (snapshot.total_calls, snapshot.failover_events) == (len(calls), failovers)


@pytest.mark.parametrize(
    "script",
    [
        [DOWN, DOWN, DOWN, "never sent"],
        [DOWN, LIMITED, DOWN, "never sent"],
        [DOWN, GARBLED, GARBLED, "never sent"],
        [DOWN, DOWN, LIMITED, "never sent"],
    ],
    ids=["down-down-down", "down-limited-down", "down-garbled-garbled", "down-down-limited"],
)
async def test_complete_makes_at_most_three_calls_and_returns_the_fallback_failure(
    script: list[str | ProviderFailure],
) -> None:
    adapter = ScriptedAdapter(script)

    result = await _gateway(adapter).complete(MESSAGES)

    assert adapter.calls == [PRIMARY, PRIMARY, FALLBACK]
    assert result == script[2]


async def test_a_rejected_retry_of_the_primary_is_returned_without_failover() -> None:
    rejected = Rejected(detail="HTTP 400: bad model")
    adapter = ScriptedAdapter([DOWN, rejected])

    result = await _gateway(adapter).complete(MESSAGES)

    assert result == rejected
    assert adapter.calls == [PRIMARY, PRIMARY]


async def test_a_cooling_primary_is_skipped_until_its_window_ends() -> None:
    clock = FakeClock()
    adapter = ScriptedAdapter([LIMITED, "failover", "skip", DOWN])
    gateway = LLMGateway(adapter, primary=PRIMARY, fallback=FALLBACK, label="test", clock=clock)
    await gateway.complete(MESSAGES)

    skipped = await gateway.complete(MESSAGES)
    fallback_down = await gateway.complete(MESSAGES)
    clock.now += settings.llm_default_cooldown_s
    after_window = await gateway.complete(MESSAGES)

    assert skipped == Completion(text="skip", model=FALLBACK)
    assert fallback_down == DOWN
    assert isinstance(after_window, Completion)
    assert after_window.model == PRIMARY
    assert adapter.calls == [PRIMARY, FALLBACK, FALLBACK, FALLBACK, PRIMARY]
    snapshot = gateway.snapshot()
    assert (snapshot.failover_events, snapshot.rate_limit_hits) == (2, 1)
    assert snapshot.active_model == PRIMARY


async def test_a_call_cancelled_after_a_429_records_nothing() -> None:
    class HangsOnFallback(ScriptedAdapter):
        async def call(
            self, model: str, messages: list[BaseMessage]
        ) -> Completion | ProviderFailure:
            if model == FALLBACK:
                await asyncio.Event().wait()
            return await super().call(model, messages)

    adapter = HangsOnFallback([LIMITED])
    gateway = _gateway(adapter)
    task = asyncio.create_task(gateway.complete(MESSAGES))
    for _ in range(5):
        await asyncio.sleep(0)
    assert adapter.calls == [PRIMARY]
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = gateway.snapshot()
    assert (snapshot.total_calls, snapshot.rate_limit_hits, snapshot.failover_events) == (0, 0, 0)
    assert snapshot.last_error is None
    assert isinstance(await gateway.complete(MESSAGES), Completion)
    assert adapter.calls[-1] == PRIMARY  # no cooldown was started
