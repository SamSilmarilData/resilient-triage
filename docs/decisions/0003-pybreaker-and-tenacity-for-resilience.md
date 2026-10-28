# ADR-003: Pybreaker and Tenacity for Fault-Tolerant Upstream Telemetry

## Status
Accepted

## Date
2026-10-10

## Context
When incident triage is dispatched, the agent queries upstream telemetry sources (e.g., Atlassian Statuspage APIs, microservice health endpoints, chaos probe simulators). During catastrophic infrastructure outages, these upstream services often exhibit:
- Total service unavailability (HTTP 502, 503, 504 errors).
- Socket hangs and connection timeouts.
- Throttling and rate-limiting (HTTP 429).

If external calls are made unguarded:
1. Sockets hang until default client timeouts expire (e.g., 30+ seconds), rendering the triage system unresponsive.
2. Repeated calls to a crashing upstream service create a "thundering herd" effect, exacerbating the upstream outage.
3. Unhandled network exceptions bubble up to users as HTTP 500 errors.

## Decision
We adopted a dual-layer resilience architecture combining **Pybreaker (>=1.3.0,<1.5)** for circuit breaking and **Tenacity (8.2+)** for adaptive retries with exponential backoff and randomized jitter:

1. **Circuit Breakers via Pybreaker**:
   - Every external telemetry integration is managed by a centralized `CircuitBreakerRegistry`.
   - Breakers trip `OPEN` after `fail_max = 3` consecutive failures within a 60-second window.
   - When `OPEN`, subsequent calls fail fast in $< 0.01\text{ms}$ by raising `BreakerOpen` (the project exception defined in `resilience/circuit_breaker.py`, the only module that imports pybreaker), instantly routing execution to `DegradedFallbackNode`.
   - Breakers transition to `HALF_OPEN` after `reset_timeout = 30.0s`, allowing a single probe request through to verify upstream recovery before closing.
2. **Exponential Backoff with Randomized Jitter via Tenacity**:
   - The `@with_retry` decorator wraps calls with randomized exponential backoff:
     $$\text{wait} = \text{random.uniform}(\text{min\_wait}, \text{min}(\text{max\_wait}, \text{multiplier} \times 2^{\text{attempt}}))$$
   - Prevents synchronized retry storms across concurrent incident investigations.
   - Restricts retries exclusively to transient exceptions via `is_transient_error()` (`httpx.TimeoutException`, `httpx.NetworkError`, HTTP 429, 502, 503, 504).
3. **Graceful State Degradation**:
   - Upstream outages never raise 500 errors to the client.
   - Tripped circuit breakers (targeted tools whose breaker is open) are explicitly annotated in the report by report reconciliation in `finalize`: one tripped breaker gives `degradation_status = PARTIALLY_DEGRADED`, two or more give `SEVERELY_DEGRADED` (e.g. `circuit_breakers_tripped = ["chaos", "statuspage"]`), and a synthesis fallback is always `SEVERELY_DEGRADED`.

## Alternatives Considered

### Standard HTTP Timeouts Only
- **Pros**: Built into `httpx.Client(timeout=5.0)`.
- **Cons**: Every request still waits 5 seconds before failing; 100 concurrent requests each wait 5 seconds, causing gateway resource exhaustion; no circuit state memory across requests.
- **Rejected**: Fails fast-recovery requirements and overwhelms failing dependencies.

### Netflix Hystrix / Java Resilience4j Ports
- **Pros**: Enterprise battle-tested patterns.
- **Cons**: Many Python ports are unmaintained or require complex multi-threaded background pools that conflict with modern `asyncio` event loops.
- **Rejected**: `pybreaker` is lightweight, active, and natively supports async listener hooks.

## Consequences
- **Guaranteed Fast Fail**: Once a dependency fails 3 times, subsequent requests bypass the dependency in $<0.01\text{ms}$, keeping overall triage latency $<1.5\text{s}$ even during total upstream collapse.
- **Self-Healing Recovery**: Breakers automatically test recovery after 30 seconds without requiring manual administrator intervention or service restarts.
- **Transparent SRE Visibility**: Incident responders are explicitly informed which monitoring systems were offline during report generation, preventing false assumptions of healthy microservices.
