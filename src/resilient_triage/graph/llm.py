"""LLM client factory and mock model for deterministic simulation and tests."""

import json
import os
from typing import Any, List, Optional

from langchain_core.callbacks.manager import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


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


def get_triage_llm() -> BaseChatModel:
    """Factory returning active chat model: test override, real provider, or mock default."""
    if _override_llm is not None:
        return _override_llm

    if os.environ.get("OPENAI_API_KEY"):
        try:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model="gpt-4o", temperature=0)
        except ImportError:
            pass

    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(model="gemini-1.5-pro", temperature=0)
        except ImportError:
            pass

    # Default to deterministic mock model
    return MockTriageChatModel()
