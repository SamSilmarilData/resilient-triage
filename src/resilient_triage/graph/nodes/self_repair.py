"""Node: self_repair - Prepares corrective prompt for LLM retry cycle."""

from typing import Any
from langchain_core.messages import HumanMessage

from resilient_triage.graph.prompts import format_self_repair_prompt
from resilient_triage.schemas.state import TriageState


def self_repair_node(state: TriageState) -> dict[str, Any]:
    """Append Pydantic ValidationError traceback to messages to guide self-repair."""
    candidate_raw = state.get("candidate_report_raw", "")
    errors = state.get("validation_errors", [])

    corrective_prompt = format_self_repair_prompt(
        candidate_raw=candidate_raw,
        validation_errors=errors,
    )

    return {
        "messages": [HumanMessage(content=corrective_prompt)],
    }
