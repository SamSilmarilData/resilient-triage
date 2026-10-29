"""Node: generate_report - Invokes LLM to produce candidate report JSON."""

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

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
            "provider_failure": None,
        }
    logger.error(
        "LLM provider failure in generate_report_node: %s: %s", type(result).__name__, result.detail
    )
    return {"candidate_report_raw": "", "provider_failure": result}
