"""Node: generate_report - Invokes LLM to produce candidate report JSON."""

from typing import Any
from langchain_core.messages import AIMessage

from resilient_triage.graph.llm import get_triage_llm
from resilient_triage.schemas.state import TriageState


async def generate_report_node(state: TriageState) -> dict[str, Any]:
    """Call LLM with accumulated messages to generate candidate triage JSON."""
    llm = get_triage_llm()
    messages = state.get("messages", [])

    response = await llm.ainvoke(messages)
    raw_content = response.content if hasattr(response, "content") else str(response)

    return {
        "candidate_report_raw": raw_content,
        "messages": [AIMessage(content=raw_content)],
    }
