# ADR-001: LangGraph Cyclic State Machine for Incident Triage

## Status
Accepted

## Date
2026-10-10

## Context
Incident triage in production SRE environments is non-linear and inherently turbulent:
- External telemetry signals (status pages, metrics, health probes) may be throttled, degraded, or offline.
- Large language models (LLMs) can produce formatting defects, invalid JSON syntax, or hallucinated fields under pressure.
- Ambiguous incident queries require iterative reflection when initial confidence is low.

Traditional linear LLM chains (such as basic sequential prompt chaining or Directed Acyclic Graphs without cycles) cannot recover when validation fails or when confidence drops below safe operational thresholds. Conversely, unconstrained multi-agent frameworks (e.g., CrewAI, AutoGen) introduce non-deterministic execution times, uncontrollable token spend, and debugging opacity unacceptable for time-critical incident response.

We required an orchestration framework that:
1. Supports cyclic graphs with conditional branching and self-healing feedback loops.
2. Guarantees deterministic cycle bounds to prevent infinite inference loops.
3. Maintains structured, typed state across all transitions.
4. Operates with full asynchronous (`async/await`) native concurrency.

## Decision
We adopted **LangGraph (0.2+)** as the core orchestration framework for `resilient-triage`.

The agent is designed as a cyclic `StateGraph(TriageState)` containing 11 specialized nodes connected by deterministic conditional routing edges:
- **`analyze_query`**: Ingests incident description and scopes telemetry targets.
- **`collect_telemetry`**: Gathers statuspage and chaos probe metrics concurrently via `asyncio.gather`.
- **`degraded_fallback`**: Intercepts tripped circuit breakers and annotates blind spots.
- **`compensate_blindspots`**: Queries semantic cache for historical incident precedents to fill missing signal gaps.
- **`synthesize_telemetry`**: Compiles signals, warnings, and breaker statuses into structured model context.
- **`generate_report`**: Dispatches context to high-speed Groq LPUs (`qwen/qwen3.8-27b`).
- **`validate_report`**: Strictly validates candidate JSON against the Pydantic `IncidentTriageReport` contract.
- **`self_repair`**: Feeds `ValidationError` tracebacks back to the LLM to self-correct invalid schemas within 2 retry attempts.
- **`refine_reflection`**: Triggers a reflection pass if confidence score is low ($< 0.60$).
- **`fallback_report`**: Constructs a deterministic safe report if retries are exhausted.
- **`finalize`**: Enforces programmatic circuit breaker consistency and commits verified reports to the semantic cache.

## Alternatives Considered

### Linear LangChain Expression Language (LCEL) Chains
- **Pros**: Simple, low conceptual overhead.
- **Cons**: Cannot execute cyclic self-repair loops when Pydantic parsing fails; requires brittle imperative error handling outside the chain.
- **Rejected**: A single formatting glitch in LLM output crashes the triage pipeline.

### Autonomous Agent Swarms (CrewAI / AutoGen)
- **Pros**: Emergent problem-solving capabilities across multiple personas.
- **Cons**: Non-deterministic execution paths; agent conversations can spin for 30+ seconds; high token consumption; unpredictable latency.
- **Rejected**: SRE triage requires sub-2 second turnaround and predictable, reproducible outputs.

### Celery / Temporal Task Workflows
- **Pros**: Industrial-grade distributed workflow orchestration.
- **Cons**: Heavy infrastructure footprint (brokers, workers, databases); overkill for single-request sub-2s inference pipelines.
- **Rejected**: Unnecessary complexity and deployment overhead for lightweight edge gateway deployment.

## Consequences
- **Deterministic SLA**: Maximum execution cycle is strictly bounded (max 2 self-repair retries, max 1 reflection cycle).
- **Zero Crashes on Bad Output**: Even if an LLM repeatedly outputs invalid JSON, the cyclic state machine cleanly drops to `fallback_report_node` and returns HTTP 200 with actionable diagnostic steps.
- **State Transparency**: `TriageState` TypedDict provides complete auditability of messages, telemetry payloads, circuit breaker flags, and repair attempts at every stage of execution.
