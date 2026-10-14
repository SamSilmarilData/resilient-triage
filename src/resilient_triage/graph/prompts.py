"""Prompt templates and formatters for LangGraph triage nodes."""

import json
from typing import Any

from resilient_triage.schemas.incident import IncidentTriageReport

TRIAGE_SYSTEM_PROMPT = """You are an expert site reliability engineer and incident triage agent named resilient-triage.
Your job is to analyze telemetry signals, isolate the root cause, determine severity, and recommend actionable remediation steps.

OUTPUT REQUIREMENTS:
You MUST respond with a single, strictly valid JSON object matching the IncidentTriageReport schema below.
DO NOT wrap your JSON in any conversational text. DO NOT invent fields outside the schema.

SCHEMA SPECIFICATION:
{
  "summary": "Clear executive summary of the incident (min 5 chars)",
  "severity": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "UNKNOWN",
  "root_cause_analysis": "Detailed diagnosis or probable failure mechanism (min 5 chars)",
  "affected_services": [
    {
      "service_name": "string",
      "impact_level": "string",
      "details": "string"
    }
  ],
  "telemetry_signals": [
    {
      "source": "string",
      "status": "string",
      "latency_ms": float or null,
      "error_rate": float or null,
      "raw_snippet": "string or null"
    }
  ],
  "recommended_actions": [
    {
      "priority": "P0" | "P1" | "P2" | "P3",
      "action": "Concrete command or instruction",
      "target_system": "Target component or service",
      "action_type": "diagnostic" | "remediative",
      "requires_approval": true | false
    }
  ],
  "degradation_status": "HEALTHY" | "PARTIALLY_DEGRADED" | "SEVERELY_DEGRADED" | "UNKNOWN",
  "circuit_breakers_tripped": ["list of tripped breaker names, or empty"],
  "confidence_score": 0.95
}

SAFETY RULES:
- Read-only diagnostic commands (e.g. log inspection, metrics checking) MUST be tagged with action_type="diagnostic" and requires_approval=false.
- Destructive, state-mutating, or traffic-shifting commands (e.g. cluster restart, traffic drain, failover) MUST be tagged with action_type="remediative" and requires_approval=true.

ROOT CAUSE DISAMBIGUATION RULES:
- REAL OUTAGES BY DEFAULT: Treat every incident inquiry as a genuine production infrastructure outage unless the inquiry explicitly specifies an intentional test or chaos drill (e.g., "chaos experiment", "drill", "game day", "simulated outage").
- SENSORS VS ROOT CAUSES: Telemetry probes (e.g., api_gateway_probe, postgres_health_probe, auth_service_probe, cluster_health_probe) and circuit breakers (e.g., Cluster Sentinel [chaos]) are observability sensors reporting symptoms, NOT root causes. If a probe reports 503 or a circuit breaker trips open, that is an indicator of downstream distress or circuit protection, never the underlying cause.
- DOMAIN-SPECIFIC MECHANISMS: Diagnose the technical failure mechanism (e.g., database connection pool exhaustion, memory pressure / OOM, worker thread saturation, network partition, upstream dependency timeout, TLS certificate expiry, deadlocks) based on inquiry context and telemetry.
- ACTIONABLE REMEDIATION: Prescribe authentic production operational commands and runbooks (e.g., restart pods, scale replicas, increase pool limits, roll back deployments, failover read-replicas) rather than referencing chaos simulation knobs.
"""


def format_telemetry_prompt(
    query: str,
    signals: list[dict[str, Any]],
    circuit_status: dict[str, str],
    degradation_note: str | None = None,
    historical_context: list[str] | None = None,
) -> str:
    """Format prompt consolidating query, telemetry signals, circuit breaker states, and blind spots."""
    lines = [f"INCIDENT INQUIRY: {query}\n"]

    if degradation_note:
        lines.append(f"⚠️ RESILIENCE DEGRADATION WARNING: {degradation_note}\n")

    lines.append("CIRCUIT BREAKER STATES:")
    for name, state in circuit_status.items():
        lines.append(f"  - {name}: {state}")

    if historical_context:
        lines.append("\nHISTORICAL CONTEXT (Compensatory Cache Retrieval):")
        for ctx in historical_context:
            lines.append(f"  - {ctx}")

    lines.append("\nLIVE TELEMETRY SIGNALS:")
    if not signals:
        lines.append("  (No live telemetry signals available)")
    else:
        for s in signals:
            src = s.get("source", "unknown")
            st = s.get("status", "unknown")
            lat = s.get("latency_ms")
            snip = s.get("raw_snippet", "")
            lat_str = f", latency={lat}ms" if lat is not None else ""
            lines.append(f"  - [{src}] status={st}{lat_str}: {snip}")

    lines.append("\nGenerate the complete JSON IncidentTriageReport matching the schema.")
    return "\n".join(lines)


def format_self_repair_prompt(
    candidate_raw: str,
    validation_errors: list[str],
) -> str:
    """Construct corrective prompt for Pydantic self-repair loop."""
    errors_formatted = "\n".join(f"  - {err}" for err in validation_errors)
    return (
        f"Your previous JSON output failed strict Pydantic validation with the following error(s):\n"
        f"{errors_formatted}\n\n"
        f"PREVIOUS INVALID CANDIDATE:\n"
        f"{candidate_raw}\n\n"
        f"CORRECTION DIRECTIVE:\n"
        f"Fix the reported errors while preserving unaffected fields.\n"
        f"Ensure all enums (severity, priority, degradation_status, action_type) are valid.\n"
        f"Output ONLY the corrected valid JSON object."
    )


def format_refine_reflection_prompt(
    current_report_json: str,
    confidence_score: float,
) -> str:
    """Construct reflection prompt when confidence score is low (<0.60)."""
    return (
        f"Your preliminary triage report produced a low confidence score of {confidence_score:.2f}.\n"
        f"PRELIMINARY REPORT:\n{current_report_json}\n\n"
        f"CONFIDENCE REFINEMENT DIRECTIVE:\n"
        f"1. Deepen your root-cause hypothesis and assess potential cascading dependencies.\n"
        f"2. Add explicit diagnostic verification commands (action_type='diagnostic', requires_approval=false).\n"
        f"3. Refine remediation steps with proper risk flags (requires_approval=true for mutating actions).\n"
        f"4. Re-calibrate confidence_score based on your deeper analysis.\n"
        f"Output ONLY the updated, valid JSON IncidentTriageReport."
    )
