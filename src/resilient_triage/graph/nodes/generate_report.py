"""Node: generate_report - Invokes LLM to produce candidate report JSON."""

import logging
from typing import Any

from langchain_core.messages import AIMessage

from resilient_triage.config import settings
from resilient_triage.graph.llm import get_triage_llm
from resilient_triage.schemas.state import TriageState

logger = logging.getLogger(__name__)


async def generate_report_node(state: TriageState) -> dict[str, Any]:
    """Call LLM with accumulated messages to generate candidate triage JSON."""
    llm = get_triage_llm()
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
