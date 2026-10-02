"""LLM client factory and mock model for deterministic simulation and tests."""

import json
import logging
import os
from typing import Any, List, Optional

import httpx
from langchain_core.callbacks.manager import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from resilient_triage.config import settings

logger = logging.getLogger(__name__)


_groq_async_client: httpx.AsyncClient | None = None


def get_groq_async_client(timeout: float = 20.0) -> httpx.AsyncClient:
    """Return persistent, keep-alive pooled httpx.AsyncClient for Groq LPU calls."""
    global _groq_async_client
    if _groq_async_client is None or _groq_async_client.is_closed:
        _groq_async_client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _groq_async_client


class GroqChatModel(BaseChatModel):
    """Native high-speed Groq LPU chat model client supporting structured JSON generation."""

    api_key: str
    model_name: str = "qwen/qwen3.8-27b"
    timeout: float = 20.0

    def _format_messages(self, messages: List[BaseMessage]) -> list[dict[str, str]]:
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
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = {
            "model": self.model_name,
            "messages": self._format_messages(messages),
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 650,
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = {
            "model": self.model_name,
            "messages": self._format_messages(messages),
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 650,
        }

        from resilient_triage.resilience.retry import with_retry

        @with_retry(max_attempts=3, min_wait=0.2, max_wait=2.0)
        async def _send_request() -> str:
            client = get_groq_async_client(timeout=self.timeout)
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
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
                        "action": "Inspect Actions webhook runner health: kubectl get pods -n actions",
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
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
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
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "mock-triage-llm"


_override_llm: BaseChatModel | None = None


def set_triage_llm(llm: BaseChatModel | None) -> None:
    """Set global override LLM (primarily for test execution)."""
    global _override_llm
    _override_llm = llm


def get_active_model_name() -> str:
    """Return friendly display label of currently active model engine."""
    if _override_llm is not None:
        return f"Test Override ({getattr(_override_llm, '_llm_type', 'mock')})"

    groq_key = settings.groq_api_key or os.environ.get("GROQ_API_KEY")
    if groq_key:
        return f"Groq LPU ({settings.groq_model})"

    if os.environ.get("OPENAI_API_KEY"):
        return "OpenAI GPT-4o"

    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        return "Google Gemini 1.5"

    return "Zero-Config SRE Simulation"


def get_triage_llm() -> BaseChatModel:
    """Factory returning active chat model: test override, Groq, real provider, or mock default."""
    if _override_llm is not None:
        return _override_llm

    # 1. Groq LPU (Sub-1.5s SOTA inference)
    groq_key = settings.groq_api_key or os.environ.get("GROQ_API_KEY")
    if groq_key:
        return GroqChatModel(
            api_key=groq_key,
            model_name=settings.groq_model,
        )

    # 2. OpenAI Provider
    if os.environ.get("OPENAI_API_KEY"):
        try:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model="gpt-4o", temperature=0)
        except ImportError:
            pass

    # 3. Google Gemini Provider
    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(model="gemini-1.5-pro", temperature=0)
        except ImportError:
            pass

    # 4. Default to deterministic mock model
    return MockTriageChatModel()
