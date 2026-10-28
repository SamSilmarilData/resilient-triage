"""Node: generate_report - Invokes LLM to produce candidate report JSON."""

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from resilient_triage.config import settings
from resilient_triage.graph.deps import TriageDeps
from resilient_triage.schemas.state import TriageState

logger = logging.getLogger(__name__)


async def generate_report_node(state: TriageState, runtime: Runtime[TriageDeps]) -> dict[str, Any]:
    """Call LLM with accumulated messages to generate candidate triage JSON."""
    llm = runtime.context.llm
    messages = state.get("messages", [])

    try:
        response = await llm.ainvoke(messages)
        raw_content = response.content if hasattr(response, "content") else str(response)
        return {
            "candidate_report_raw": raw_content,
            "messages": [AIMessage(content=raw_content)],
        }
    except Exception as exc:
        logger.error("LLM inference call failed in generate_report_node: %s", exc)
        return {
            "candidate_report_raw": "",
            "validation_errors": [f"LLM Provider Failure: {exc}"],
            "repair_attempts": settings.max_self_repair_retries,
        }
