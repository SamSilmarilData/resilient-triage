"""Node: generate_report - Invokes LLM to produce candidate report JSON."""

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from resilient_triage.config import settings
from resilient_triage.graph.deps import TriageDeps
from resilient_triage.llm import Completion
from resilient_triage.schemas.state import TriageState

logger = logging.getLogger(__name__)


async def generate_report_node(state: TriageState, runtime: Runtime[TriageDeps]) -> dict[str, Any]:
    """Call LLM with accumulated messages to generate candidate triage JSON."""
    result = await runtime.context.llm.complete(state.get("messages", []))
    if isinstance(result, Completion):
        return {
            "candidate_report_raw": result.text,
            "messages": [AIMessage(content=result.text)],
        }
    kind = type(result).__name__
    logger.error("LLM provider failure in generate_report_node: %s: %s", kind, result.detail)
    # Today's state shape for a provider failure, so the graph routes to fallback_report (#15).
    return {
        "candidate_report_raw": "",
        "validation_errors": [f"LLM Provider Failure: {kind}: {result.detail}"],
        "repair_attempts": settings.max_self_repair_retries,
    }
