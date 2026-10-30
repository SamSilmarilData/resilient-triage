# resilient-triage

A fault-tolerant incident triage gateway: it turns an incident query into a strict triage report, and degrades rather than fails when its dependencies are down.

## Language

### Degradation

**Tripped breaker**:
A circuit breaker that is refusing calls *and* is targeted by the current query, as observed when telemetry is collected. An open breaker the query never targets is not tripped for that report.
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

### Request path

**Triage outcome**:
How a triage request ended: answered from the cache, answered by a run, or shed. A run that ends in a synthesis fallback is still an answer, not a separate outcome.
_Avoid_: result, response (that is the HTTP layer)

**Shed request**:
A triage request refused because no run slot freed up in time. The caller is told to retry later; no report is produced.
_Avoid_: saturated (except as the existing metric label), rejected, throttled

**Run deadline**:
The longest a single run may take. When it passes, the request is answered with a synthesis fallback instead of waiting further.
_Avoid_: timeout (unqualified), request timeout

### Telemetry

**Telemetry source**:
A system the agent reads incident signals from: Statuspage (external) or Cluster Sentinel (internal).
_Avoid_: tool, client, probe (when meaning the source)

**Targeted source**:
A telemetry source the current query was routed to read. Only targeted sources can make a report degraded.
_Avoid_: target tool, selected tool

**Cluster Sentinel**:
The internal cluster-health telemetry source.
_Avoid_: chaos, chaos probe (when meaning the source)

**Chaos injection**:
Faults an operator deliberately applies to Cluster Sentinel (latency, forced or random failures) to exercise degradation.
_Avoid_: chaos (unqualified), simulation

### Execution trace

**Execution trace**:
The ordered record of what happened while one triage request was handled, from the cache lookup to its triage outcome, including the work done outside the run.
_Avoid_: telemetry (that is incident signals), log, history

**Trace event**:
One entry in an execution trace.
_Avoid_: step, span, log line

**Trace summary**:
The condensed execution trace returned with every triage answer.
_Avoid_: trace (when meaning the summary)

**Abandoned request**:
A triage request whose caller disconnected before its outcome. It has no triage outcome and leaves nothing in the cache.
_Avoid_: cancelled request, shed request (that is a refusal)

### Demonstration

**Scenario run**:
A run requested by naming a catalogued scenario, whose model replies and Statuspage readings are scripted by that scenario. It never calls the real model, never touches the shared cache, and never counts as the deployment's real traffic. The rest of the run (graph, reconciliation, cache logic) is real.
_Avoid_: demo mode, demo run, simulation (that is the keyless model stand-in)

**Free play**:
A visitor's own query, answered exactly as any triage request is, with the real model when one is configured.
_Avoid_: live mode, sandbox

**Scenario seed**:
An answer placed in a scenario run's private cache before the run starts, so the scenario can show a cache hit independently of what any earlier run did.
_Avoid_: warm-up (that is embedding pre-warming), fixture
