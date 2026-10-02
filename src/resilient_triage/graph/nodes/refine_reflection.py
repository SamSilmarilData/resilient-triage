"""Node: refine_reflection - Prompts LLM for confidence-driven reflection when confidence is low."""

from typing import Any
from langchain_core.messages import HumanMessage

from resilient_triage.graph.prompts import format_refine_reflection_prompt
from resilient_triage.schemas.state import TriageState


def refine_reflection_node(state: TriageState) -> dict[str, Any]:
    """Trigger reflection cycle to deepen root-cause analysis when confidence is low (<0.60)."""
    report = state.get("final_report")
    report_json = report.model_dump_json() if report else "{}"
    conf = report.confidence_score if report else 0.0

    reflection_prompt = format_refine_reflection_prompt(
        current_report_json=report_json,
        confidence_score=conf,
    )

    return {
        "final_report": None,  # Reset to allow refined generation to pass through validate
        "refinement_attempts": state.get("refinement_attempts", 0) + 1,
        "messages": [HumanMessage(content=reflection_prompt)],
    }
