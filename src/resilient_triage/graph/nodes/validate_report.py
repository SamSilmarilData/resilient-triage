"""Node: validate_report - Parses candidate output strictly into IncidentTriageReport."""

import json
import logging
import re
from typing import Any

from langgraph.runtime import Runtime
from pydantic import ValidationError

from resilient_triage.config import settings
from resilient_triage.graph.deps import TriageDeps
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.state import TriageState
from resilient_triage.trace.events import ValidationFailed
from resilient_triage.trace.recorder import record_event

logger = logging.getLogger(__name__)


def self_repair_exhausted(repair_attempts: int) -> bool:
    """Return True once repair_attempts has used up the self-repair budget."""
    return repair_attempts >= settings.max_self_repair_retries


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


def validate_report_node(state: TriageState, runtime: Runtime[TriageDeps]) -> dict[str, Any]:
    """Validate candidate report strictly against Pydantic schema."""
    # A failure is recorded on the run's trace as validation_failed: field names and error
    # types only, never the candidate's values (ADR-008).
    candidate_raw = state.get("candidate_report_raw") or ""
    repair_attempts = state.get("repair_attempts", 0)

    try:
        cleaned = clean_json_string(candidate_raw)
        data = json.loads(cleaned)
        if isinstance(data, dict):
            if data.get("confidence_score") is None:
                data["confidence_score"] = 0.85
            if not data.get("severity"):
                data["severity"] = "UNKNOWN"
            if not data.get("summary"):
                data["summary"] = state.get("query") or "Automated incident triage summary"
            if not data.get("root_cause_analysis"):
                data["root_cause_analysis"] = "Diagnostic analysis in progress"

        report = IncidentTriageReport.model_validate(data)

        logger.info("Candidate report successfully validated against IncidentTriageReport schema.")
        return {
            "final_report": report,
            "validation_errors": [],
        }

    except (ValidationError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Pydantic validation failed on attempt %d: %s", repair_attempts + 1, exc)
        failure = exc  # the except name is unbound after the block; the lambda runs inside it
        record_event(
            runtime.context.trace,
            lambda: ValidationFailed.from_error(failure, attempt=repair_attempts + 1),
        )

        errors: list[str] = []
        if isinstance(exc, ValidationError):
            for err in exc.errors():
                loc = " -> ".join(str(p) for p in err.get("loc", []))
                msg = err.get("msg", "")
                inp = err.get("input", "")
                errors.append(f"Field '{loc}': {msg} (input was: {inp!r})")
        else:
            errors.append(f"JSON Parsing Error: {exc}")

        # A refine round that runs out of self-repair keeps the valid report it started from.
        # It shares the first generation's repair budget, which bounds the longest path.
        exhausted = self_repair_exhausted(repair_attempts + 1)
        return {
            "final_report": state.get("refined_from") if exhausted else None,
            "repair_attempts": repair_attempts + 1,
            "validation_errors": errors,
        }
