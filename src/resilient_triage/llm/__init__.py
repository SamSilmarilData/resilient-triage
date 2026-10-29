"""LLM gateway package: every model call goes through LLMGateway.complete()."""

from resilient_triage.config import Settings
from resilient_triage.llm.failures import (
    MalformedResponse,
    ProviderFailure,
    RateLimited,
    RateLimitScope,
    Rejected,
    Unavailable,
)
from resilient_triage.llm.gateway import (
    Completion,
    LLMGateway,
    ModelAdapter,
    QuotaHeaders,
)
from resilient_triage.llm.groq import GroqAdapter
from resilient_triage.llm.scripted import DEFAULT_REPORT_JSON, SIMULATION_LABEL, ScriptedAdapter

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
    "RateLimitScope",
    "RateLimited",
    "Rejected",
    "ScriptedAdapter",
    "Unavailable",
    "build_llm_gateway",
]


def build_llm_gateway(app_settings: Settings, adapter: ModelAdapter | None = None) -> LLMGateway:
    """Build the gateway; with no adapter, a Groq key selects Groq, else the simulation."""
    if adapter is None:
        if app_settings.groq_api_key:
            adapter = GroqAdapter(
                app_settings.groq_api_key, timeout_s=app_settings.llm_request_timeout_s
            )
        else:
            adapter = ScriptedAdapter()
    primary = app_settings.groq_model
    return LLMGateway(
        adapter,
        primary=primary,
        fallback=app_settings.groq_fallback_model,
        label=adapter.label(primary),
    )
