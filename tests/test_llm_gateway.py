"""Tests for the LLM gateway: provider selection, the gateway seam, and its adapters."""

import sys
import types

import pytest

from resilient_triage.config import settings
from resilient_triage.runtime import build_runtime


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

        from resilient_triage.llm import ScriptedAdapter

        assert isinstance(runtime.llm.adapter, ScriptedAdapter)
    finally:
        await runtime.aclose()
