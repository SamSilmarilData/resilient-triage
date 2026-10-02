"""Node: validate_report - Parses candidate output strictly into IncidentTriageReport."""

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.state import TriageState

logger = logging.getLogger(__name__)


def clean_json_string(raw: str) -> str:
    """Strip markdown codeblock wrappers and extract the outermost JSON object."""
    text = raw.strip()

    # 1. Strip markdown fences like ```json ... ```
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # 2. Extract outermost JSON object if enclosed in conversational text
    start_idx = text.find("{")
    end_idx = text.rfind("}")

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return text[start_idx : end_idx + 1]

    return text


def validate_report_node(state: TriageState) -> dict[str, Any]:
    """Validate candidate report strictly against Pydantic schema."""
    candidate_raw = state.get("candidate_report_raw", "")
    repair_attempts = state.get("repair_attempts", 0)

    try:
        cleaned = clean_json_string(candidate_raw)
        report = IncidentTriageReport.model_validate_json(cleaned)

        # Enforce consistency with circuit breaker status from state machine
        cb_status = state.get("circuit_breaker_status", {})
        tripped = [name for name, st in cb_status.items() if st == "open"]
        if tripped:
            report.circuit_breakers_tripped = list(set(report.circuit_breakers_tripped + tripped))

        logger.info("Candidate report successfully validated against IncidentTriageReport schema.")
        return {
            "final_report": report,
            "validation_errors": [],
        }

    except (ValidationError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Pydantic validation failed on attempt %d: %s", repair_attempts + 1, exc)

        errors = []
        if isinstance(exc, ValidationError):
            for err in exc.errors():
                loc = " -> ".join(str(p) for p in err.get("loc", []))
                msg = err.get("msg", "")
                inp = err.get("input", "")
                errors.append(f"Field '{loc}': {msg} (input was: {inp!r})")
        else:
            errors.append(f"JSON Parsing Error: {exc}")

        return {
            "final_report": None,
            "repair_attempts": repair_attempts + 1,
            "validation_errors": errors,
        }
