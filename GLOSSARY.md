# resilient-triage

A fault-tolerant incident triage gateway: it turns an incident query into a strict triage report, and degrades rather than fails when its dependencies are down.

## Language

### Degradation

**Tripped breaker**:
A circuit breaker that is open *and* targeted by the current query, as observed when telemetry is collected. An open breaker the query never targets is not tripped for that report.
_Avoid_: open breaker (when meaning the per-report set), failed breaker

**Degradation status**:
How impaired the triage agent itself was while producing a report: HEALTHY, PARTIALLY_DEGRADED or SEVERELY_DEGRADED. A system fact, never the model's opinion, and never lowered by a later stage.
_Avoid_: health, agent mode

**Synthesis fallback**:
A run whose report was built deterministically because the model could not produce a valid one (self-repair limit reached or provider failure).
_Avoid_: fallback (unqualified), degraded fallback (that is the telemetry branch)

**Unavailable source**:
A targeted telemetry source that could not be read during a run although its breaker had not tripped.
_Avoid_: failed probe, error signal

**Degradation evidence**:
The facts about a run that determine its degradation status: its tripped breakers, its unavailable sources, and whether it was a synthesis fallback.
_Avoid_: breaker status, degradation reason

**Report reconciliation**:
The single step that makes a report's system facts (degradation status, tripped breakers, approval requirements) agree with what actually happened during the run, regardless of which path produced the report.
_Avoid_: finalization, consistency enforcement, post-processing

### Model access

**Provider failure**:
A model call that produced no text: the provider rate-limited it, was unavailable, rejected it, or answered with something unreadable. Distinct from a completion whose text is not a valid report, which is a self-repair matter, not a provider failure.
_Avoid_: LLM error, inference failure, invalid output (when meaning a provider failure)

**Model failover**:
Answering a call with the fallback model because the primary model is rate-limited or failing. Each model has its own cooldown; one model's limits never cool down another.
_Avoid_: fallback (unqualified), synthesis fallback (that is the deterministic report)

### Caching

**Service scope**:
The service a query is constrained to, or no service at all. Part of a cached answer's identity: a query reuses only answers given under exactly the same scope, and "no scope" is a scope of its own, not a wildcard.
_Avoid_: service filter (the request field that sets it), component
