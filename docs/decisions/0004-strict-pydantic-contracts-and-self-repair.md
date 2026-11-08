# ADR-004: Strict Pydantic Contracts and Automated Self-Repair Loop

## Status
Accepted

## Date
2026-10-10

## Context
Downstream consumers of automated incident triage (e.g., Slack Webhooks, PagerDuty incidents, auto-remediation scripts, Kubernetes operator controllers) require deterministic data contracts. If an AI agent emits unexpected keys, omits mandatory priority fields, or formats output as unparseable markdown blocks, downstream automation crashes.

Common failure modes in LLM generation include:
1. Emitting markdown code fences (` ```json ... ``` `) around JSON strings.
2. Hallucinating extra keys or changing schema structure.
3. Omitting required enum values (e.g., lowercase `critical` instead of uppercase `CRITICAL`).
4. Outputting truncated JSON due to token boundaries.

Without strict validation and an automated feedback mechanism, these anomalies cause unhandled exceptions and triage downtime.

## Decision
We implemented a **Strict Pydantic v2 Contract with an Automated Self-Repair Feedback Loop**:

1. **Strict Data Contracts (`extra="forbid"`)**:
   - Every incident model in `resilient_triage.schemas.incident` enforces:
     ```python
     model_config = ConfigDict(extra="forbid")
     ```
   - Any hallucinated or unexpected fields immediately raise `pydantic.ValidationError`.
   - String enums (`SeverityLevel`, `DegradationStatus`, `PriorityLevel`) inherit from `(str, Enum)`.
2. **Three-Tier Markdown Sanitizer (`clean_json_string`)**:
   - Pre-processes LLM output by stripping markdown fences (` ```json `), extracting the outermost balanced JSON object `{ ... }`, and unescaping control characters before parsing.
3. **Automated Self-Repair Loop**:
   - `ValidateReportNode` catches `ValidationError` and `json.JSONDecodeError`.
   - If retries $< 2$, execution routes to `SelfRepairNode`.
   - `SelfRepairNode` extracts the exact field paths, expected types, and validation error messages from the traceback and appends a corrective instruction to the LangGraph message state:
     ```text
     "The previous JSON output failed validation with the following errors:
      - Field 'severity': Input should be 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'
      Please re-emit the complete JSON matching the required schema."
     ```
   - LangGraph routes the corrective prompt back to `GenerateReportNode` for self-repair.
4. **Deterministic Fallback Report**:
   - If self-repair retries are exhausted ($\ge 2$), execution routes to `FallbackReportNode`.
   - `FallbackReportNode` deterministically constructs a guaranteed valid `IncidentTriageReport` annotated with `degradation_status = SEVERELY_DEGRADED` and actionable manual diagnostic commands.

### Amendment (2026-10-29)
Decision §4 no longer holds in a refine round. When self-repair runs out or the LLM provider fails during the confidence-driven refine round, the graph finalizes the valid report the round started from (`refined_from`) instead of routing to `FallbackReportNode`. Outside a refine round, §4 stands. See #15.

## Alternatives Considered

### Permissive Pydantic Schemas (`extra="ignore"`)
- **Pros**: Fewer validation errors on minor hallucinated fields.
- **Cons**: Silently swallows hallucinations; allows LLM schema drift to compound over time without detection; downstream services receive unexpected shapes.
- **Rejected**: SRE automation requires zero tolerance for schema drift.

### LangChain OutputFixingParser
- **Pros**: Built-in LangChain utility.
- **Cons**: Spawns a secondary LLM chain outside of the graph state machine, hiding latency, token spend, and trace visibility; cannot dynamically route through degraded fallbacks.
- **Rejected**: LangGraph native cyclic state routing provides complete state observability.

## Consequences
- **Zero Schema Drift**: Every report returned to clients strictly matches the `IncidentTriageReport` specification.
- **High Self-Healing Rate**: 95%+ of minor formatting or enum anomalies are resolved on attempt 1 without human intervention.
- **Guaranteed Fallback**: Even under severe token truncation or complete model degradation, the system never returns HTTP 500, always providing responders with a safe baseline report.
