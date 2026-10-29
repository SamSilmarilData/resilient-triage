"""LLM gateway package: every model call goes through LLMGateway.complete()."""

import os

from resilient_triage.config import Settings
from resilient_triage.llm.gateway import (
    Completion,
    LLMGateway,
    MalformedResponse,
    ModelAdapter,
    ProviderFailure,
    QuotaHeaders,
    QuotaReporter,
    RateLimited,
    Rejected,
    Unavailable,
)
from resilient_triage.llm.groq import GroqAdapter
from resilient_triage.llm.scripted import DEFAULT_REPORT_JSON, ScriptedAdapter

__all__ = [
    "DEFAULT_REPORT_JSON",
    "SIMULATION_LABEL",
    "Completion",
    "GroqAdapter",
    "LLMGateway",
    "MalformedResponse",
    "ModelAdapter",
    "ProviderFailure",
    "QuotaHeaders",
    "QuotaReporter",
    "RateLimited",
    "Rejected",
    "ScriptedAdapter",
    "Unavailable",
    "build_llm_gateway",
]

SIMULATION_LABEL = "Zero-Config SRE Simulation"


def _label(adapter: ModelAdapter, primary: str) -> str:
    """The /health label for an adapter, so the label always names what runs."""
    if isinstance(adapter, GroqAdapter):
        return f"Groq LPU ({primary})"
    if isinstance(adapter, ScriptedAdapter):
        return SIMULATION_LABEL
    return f"Test Override ({type(adapter).__name__})"


def build_llm_gateway(app_settings: Settings, adapter: ModelAdapter | None = None) -> LLMGateway:
    """Build the gateway; with no adapter, a Groq key selects Groq, else the simulation."""
    if adapter is None:
        groq_key = app_settings.groq_api_key or os.environ.get("GROQ_API_KEY")
        if groq_key:
            adapter = GroqAdapter(groq_key, timeout_s=app_settings.llm_request_timeout_s)
        else:
            adapter = ScriptedAdapter()
    primary = app_settings.groq_model
    return LLMGateway(
        adapter,
        primary=primary,
        fallback=app_settings.groq_fallback_model,
        label=_label(adapter, primary),
    )
