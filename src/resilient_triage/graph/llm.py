"""LLM client factory and mock model for deterministic simulation and tests."""

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from langchain_core.callbacks.manager import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from resilient_triage.config import Settings, settings
from resilient_triage.schemas.api import LLMQuotaSnapshot

logger = logging.getLogger(__name__)


GROQ_TIMEOUT_SECONDS = 20.0


def new_groq_http_client(timeout: float = GROQ_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """Build a keep-alive pooled httpx.AsyncClient for Groq LPU calls (no I/O until used)."""
    return httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    )


class GroqQuotaTracker:
    """Thread-safe tracker of Groq rate-limit headers, token budgets, and failover metrics."""

    def __init__(self, active_model: str | None = None) -> None:
        self._lock = threading.Lock()
        self.provider: str = "groq"
        self.active_model: str = active_model or settings.groq_model
        self.fallback_model: str = "llama-3.1-8b-instant"
        self.remaining_requests: int | None = None
        self.limit_requests: int | None = None
        self.remaining_tokens: int | None = None
        self.limit_tokens: int | None = None
        self.reset_requests: str | None = None
        self.reset_tokens: str | None = None
        self.total_calls: int = 0
        self.rate_limit_hits: int = 0
        self.failover_events: int = 0
        self.last_status: int | None = None
        self.last_error: str | None = None
        self.last_updated: datetime | None = None
        self.rate_limit_cooldown_until: float = 0.0

    def record_response(
        self,
        resp: httpx.Response,
        model_name: str,
        is_failover: bool = False,
    ) -> None:
        with self._lock:
            self.total_calls += 1
            self.last_status = resp.status_code
            self.last_updated = datetime.now(UTC)
            self.active_model = model_name

            if is_failover and resp.status_code < 400:
                self.failover_events += 1

            if resp.status_code == 429:
                self.rate_limit_hits += 1
                try:
                    data = resp.json()
                    self.last_error = data.get("error", {}).get("message", resp.text)
                except Exception:
                    self.last_error = resp.text

                # Parse cooldown or apply default 30s window
                cooldown_sec = 30.0
                try:
                    reset_header = resp.headers.get("x-ratelimit-reset-requests")
                    if reset_header and isinstance(reset_header, str):
                        m = re.search(r"(\d+(?:\.\d+)?)\s*s", reset_header)
                        if m:
                            cooldown_sec = float(m.group(1))
                except Exception:
                    pass
                self.rate_limit_cooldown_until = time.monotonic() + cooldown_sec
            elif resp.status_code >= 400:
                try:
                    data = resp.json()
                    self.last_error = data.get("error", {}).get("message", resp.text)
                except Exception:
                    self.last_error = resp.text

            # Parse standard Groq rate limit headers
            headers = resp.headers
            if "x-ratelimit-remaining-requests" in headers:
                try:
                    self.remaining_requests = int(headers["x-ratelimit-remaining-requests"])
                except (ValueError, TypeError):
                    pass
            if "x-ratelimit-limit-requests" in headers:
                try:
                    self.limit_requests = int(headers["x-ratelimit-limit-requests"])
                except (ValueError, TypeError):
                    pass
            if "x-ratelimit-remaining-tokens" in headers:
                try:
                    self.remaining_tokens = int(headers["x-ratelimit-remaining-tokens"])
                except (ValueError, TypeError):
                    pass
            if "x-ratelimit-limit-tokens" in headers:
                try:
                    self.limit_tokens = int(headers["x-ratelimit-limit-tokens"])
                except (ValueError, TypeError):
                    pass
            if "x-ratelimit-reset-requests" in headers:
                self.reset_requests = headers["x-ratelimit-reset-requests"]
            if "x-ratelimit-reset-tokens" in headers:
                self.reset_tokens = headers["x-ratelimit-reset-tokens"]

    def reset_counters(self) -> None:
        with self._lock:
            self.total_calls = 0
            self.rate_limit_hits = 0
            self.failover_events = 0
            self.last_error = None
            self.rate_limit_cooldown_until = 0.0

    def is_primary_rate_limited(self) -> bool:
        """Return True if primary model is within active rate-limit cooldown window."""
        with self._lock:
            return time.monotonic() < self.rate_limit_cooldown_until

    def get_snapshot(self) -> LLMQuotaSnapshot:
        with self._lock:
            return LLMQuotaSnapshot(
                provider=self.provider,
                active_model=self.active_model,
                fallback_model=self.fallback_model,
                remaining_requests=self.remaining_requests,
                limit_requests=self.limit_requests,
                remaining_tokens=self.remaining_tokens,
                limit_tokens=self.limit_tokens,
                reset_requests=self.reset_requests,
                reset_tokens=self.reset_tokens,
                total_calls=self.total_calls,
                rate_limit_hits=self.rate_limit_hits,
                failover_events=self.failover_events,
                last_status=self.last_status,
                last_error=self.last_error,
                last_updated=self.last_updated,
            )


class GroqChatModel(BaseChatModel):
    """Native high-speed Groq LPU chat model client supporting structured JSON generation."""

    # A misspelled keyword would otherwise be dropped and fall back to a default.
    model_config = ConfigDict(extra="forbid")

    api_key: str
    model_name: str = "qwen/qwen3.8-27b"
    fallback_model: str = "llama-3.1-8b-instant"
    timeout: float = GROQ_TIMEOUT_SECONDS
    quota_tracker: GroqQuotaTracker = Field(exclude=True)
    http_client: httpx.AsyncClient = Field(exclude=True)

    def _format_messages(self, messages: list[BaseMessage]) -> list[dict[str, str]]:
        formatted = []
        for m in messages:
            if m.type == "human":
                role = "user"
            elif m.type == "ai":
                role = "assistant"
            elif m.type == "system":
                role = "system"
            else:
                role = "user"
            formatted.append({"role": role, "content": str(m.content)})
        return formatted

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        use_fallback_directly = bool(
            self.fallback_model and self.quota_tracker.is_primary_rate_limited()
        )
        target_model = self.fallback_model if use_fallback_directly else self.model_name
        payload = {
            "model": target_model,
            "messages": self._format_messages(messages),
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 2500,
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            self.quota_tracker.record_response(
                resp, model_name=target_model, is_failover=use_fallback_directly
            )

            if resp.status_code == 429 and self.fallback_model and not use_fallback_directly:
                logger.warning(
                    "Groq rate limit (429) hit for %s; failing over to %s",
                    self.model_name,
                    self.fallback_model,
                )
                fb_payload = dict(payload)
                fb_payload["model"] = self.fallback_model
                fb_resp = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=fb_payload,
                )
                self.quota_tracker.record_response(
                    fb_resp, model_name=self.fallback_model, is_failover=True
                )
                if fb_resp.status_code < 400:
                    logger.info(
                        "Groq fallback model '%s' succeeded (HTTP %d).",
                        self.fallback_model,
                        fb_resp.status_code,
                    )
                    content = fb_resp.json()["choices"][0]["message"]["content"]
                    return ChatResult(
                        generations=[ChatGeneration(message=AIMessage(content=content))]
                    )
                else:
                    logger.warning(
                        "Groq fallback model '%s' also failed with HTTP %d: %s",
                        self.fallback_model,
                        fb_resp.status_code,
                        fb_resp.text,
                    )

            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        use_fallback_directly = bool(
            self.fallback_model and self.quota_tracker.is_primary_rate_limited()
        )
        target_model = self.fallback_model if use_fallback_directly else self.model_name
        payload = {
            "model": target_model,
            "messages": self._format_messages(messages),
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 2500,
        }

        from resilient_triage.resilience.retry import with_retry

        @with_retry(max_attempts=2, min_wait=0.5, max_wait=3.0)
        async def _send_request() -> str:
            client = self.http_client
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            self.quota_tracker.record_response(
                resp, model_name=target_model, is_failover=use_fallback_directly
            )

            if resp.status_code == 429 and self.fallback_model and not use_fallback_directly:
                logger.warning(
                    "Groq rate limit (429) hit for %s; failing over to %s",
                    self.model_name,
                    self.fallback_model,
                )
                fb_payload = dict(payload)
                fb_payload["model"] = self.fallback_model
                fb_resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=fb_payload,
                )
                self.quota_tracker.record_response(
                    fb_resp, model_name=self.fallback_model, is_failover=True
                )
                if fb_resp.status_code < 400:
                    logger.info(
                        "Groq fallback model '%s' succeeded (HTTP %d).",
                        self.fallback_model,
                        fb_resp.status_code,
                    )
                    return fb_resp.json()["choices"][0]["message"]["content"]
                else:
                    logger.warning(
                        "Groq fallback model '%s' also failed with HTTP %d: %s",
                        self.fallback_model,
                        fb_resp.status_code,
                        fb_resp.text,
                    )

            if resp.status_code >= 400:
                logger.warning("Groq API returned HTTP %d: %s", resp.status_code, resp.text)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

        content = await _send_request()
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self) -> str:
        return "groq-lpu-chat-model"


class MockTriageChatModel(BaseChatModel):
    """Deterministic mock chat model for automated testing of LangGraph workflows.

    Supports pre-programmed response sequences (e.g. malformed output followed by repaired output).
    """

    responses: list[str] = []
    call_count: int = 0

    def _get_default_valid_response(self) -> str:
        return json.dumps(
            {
                "summary": "GitHub Actions workflows experiencing elevated failure rates",
                "severity": "HIGH",
                "root_cause_analysis": "Upstream webhook processing queue saturation",
                "affected_services": [
                    {
                        "service_name": "Actions Runner",
                        "impact_level": "Major Outage",
                        "details": "Job pickup delayed by >15 minutes",
                    }
                ],
                "telemetry_signals": [
                    {
                        "source": "statuspage:global",
                        "status": "major",
                        "latency_ms": 320.0,
                        "error_rate": 0.5,
                        "raw_snippet": "GitHub: Major Service Outage",
                    }
                ],
                "recommended_actions": [
                    {
                        "priority": "P0",
                        "action": (
                            "Inspect Actions webhook runner health: kubectl get pods -n actions"
                        ),
                        "target_system": "Actions Controller",
                        "action_type": "diagnostic",
                        "requires_approval": False,
                    },
                    {
                        "priority": "P1",
                        "action": "Scale runner replica pool by 50%",
                        "target_system": "Runner Deployment",
                        "action_type": "remediative",
                        "requires_approval": True,
                    },
                ],
                "degradation_status": "HEALTHY",
                "circuit_breakers_tripped": [],
                "confidence_score": 0.90,
            }
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.responses and self.call_count < len(self.responses):
            resp_content = self.responses[self.call_count]
        else:
            resp_content = self._get_default_valid_response()

        self.call_count += 1
        message = AIMessage(content=resp_content)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, run_manager=None, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "mock-triage-llm"


@dataclass(frozen=True)
class TriageLLM:
    """The chat model the triage graph calls, with the label shown as the active model."""

    model: BaseChatModel
    label: str


def build_triage_llm(
    app_settings: Settings,
    *,
    quota_tracker: GroqQuotaTracker,
    http_client: httpx.AsyncClient,
) -> TriageLLM:
    """Return the configured provider (Groq, OpenAI, Gemini, else mock) and its display label."""
    # 1. Groq LPU (Sub-1.5s SOTA inference)
    groq_key = app_settings.groq_api_key or os.environ.get("GROQ_API_KEY")
    if groq_key:
        groq = GroqChatModel(
            api_key=groq_key,
            model_name=app_settings.groq_model,
            quota_tracker=quota_tracker,
            http_client=http_client,
        )
        return TriageLLM(groq, f"Groq LPU ({app_settings.groq_model})")

    # 2. OpenAI Provider
    if os.environ.get("OPENAI_API_KEY"):
        try:
            from langchain_openai import ChatOpenAI

            return TriageLLM(ChatOpenAI(model="gpt-4o", temperature=0), "OpenAI GPT-4o")
        except ImportError:
            pass

    # 3. Google Gemini Provider
    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI

            gemini = ChatGoogleGenerativeAI(model="gemini-1.5-pro", temperature=0)
            return TriageLLM(gemini, "Google Gemini 1.5")
        except ImportError:
            pass

    # 4. Default to deterministic mock model
    return TriageLLM(MockTriageChatModel(), "Zero-Config SRE Simulation")
