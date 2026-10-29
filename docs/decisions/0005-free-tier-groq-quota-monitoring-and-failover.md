# ADR-005: Free-Tier Groq Quota Monitoring and Multi-Model Failover

## Status
Accepted. The fallback model and failover design are superseded by [ADR-006](0006-project-owned-llm-gateway.md).

> **Superseded in part (2026-10-28):** ADR-006 replaces this ADR's fallback model and failover design. The fallback is now `GROQ_FALLBACK_MODEL` (default `llama-3.1-8b-instant`), not `openai/gpt-oss-20b`; failover and per-model cooldowns live in the LLM gateway (`src/resilient_triage/llm/`), and the classes named below no longer exist. Header-based quota tracking and the telemetry endpoints still stand. See [docs/resilience.md](../resilience.md#6-llm-gateway-failover-and-rate-limit-cooldowns) for the current policy.

## Date
2026-10-10

## Context
`resilient-triage` utilizes Groq's high-speed Language Processing Units (LPUs) to generate incident analysis in $\approx 1.2\text{s}$. However, on Groq's free tier, models are subject to strict per-minute token and request limits:
- Primary model (`qwen/qwen3.8-27b`): Enforces a ceiling of 1,000 output tokens per minute and 30 requests per minute.
- A sudden burst of concurrent triage requests or an unusually verbose incident description can trigger HTTP 429 (`Rate limit reached`) errors mid-flight.
- Without rate limit observability and failover routing, users experience sudden outage errors when token buckets temporarily deplete.

## Decision
We implemented a **Real-Time Quota Telemetry Engine with Automated Multi-Model Failover**:

1. **Header-Based Quota Tracking (the Groq quota tracker)**:
   - A thread-safe singleton (`groq_quota_tracker`) inspects HTTP headers on every incoming response from Groq:
     - `x-ratelimit-remaining-tokens` / `x-ratelimit-limit-tokens`
     - `x-ratelimit-remaining-requests` / `x-ratelimit-limit-requests`
     - `x-ratelimit-reset-tokens` / `x-ratelimit-reset-requests`
   - Maintains rolling metrics: `total_calls`, `rate_limit_hits` (HTTP 429), `failover_events`, `last_status`, and `last_error`.
2. **Automated Secondary Model Failover**:
   - When the primary model (`qwen/qwen3.8-27b`) returns HTTP 429:
   - The Groq chat model immediately intercepts the 429 error and reroutes the identical prompt payload to a secondary independent model bucket:
     - Secondary Fallback: **`openai/gpt-oss-20b`**
   - Because `openai/gpt-oss-20b` possesses its own independent token bucket on Groq, the request succeeds immediately without waiting for the primary quota window to reset.
   - Failover events are recorded in `groq_quota_tracker`, logged at `INFO` level, and exposed to client telemetry.
3. **Enhanced Diagnostics**:
   - If both the primary and fallback models fail, the Groq chat model logs the exact HTTP status code and full response body (`fb_resp.text`) before raising, enabling instant diagnosis of upstream Groq outages or invalid tokens.
4. **Exposed Telemetry Endpoints**:
   - `GET /llm/quota`: Dedicated endpoint returning strict `LLMQuotaSnapshot`.
   - `GET /health` & `GET /resilience/status`: Embed `llm_quota` snapshot into standard health diagnostics.
   - Top navbar in the UI renders a live interactive badge (`#llm-quota-badge`) alerting users to quota depletion or active fallback operations.
   - `POST /resilience/reset`: Resets quota counters alongside circuit breakers and chaos states.

## Alternatives Considered

### Linear Sleeping on 429
- **Pros**: Eventually succeeds when window resets.
- **Cons**: Blocks requests for up to 60 seconds; violates the sub-2s incident triage SLA; exhausts gateway connection pools.
- **Rejected**: SRE incident responders cannot wait 60 seconds for an analysis report.

### Pure Client-Side Throttling
- **Pros**: Prevents sending requests when quota is low.
- **Cons**: Difficult to synchronize across multiple distributed web processes or browser tabs without shared database locks.
- **Rejected**: Upstream header inspection is authoritative and introduces zero distributed locking overhead.

## Consequences
- **Zero Downtime on Token Exhaustion**: Bursts exceeding the 1,000 token/min ceiling of the primary model seamlessly execute on the fallback model.
- **Complete Rate-Limit Transparency**: Responders can hover over the UI badge or query `/llm/quota` to inspect exact token resets and remaining capacities.
- **Resilient Fallback Logging**: Detailed error logging ensures upstream Groq anomalies are diagnosable without attaching interactive debuggers.
