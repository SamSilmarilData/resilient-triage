# Research: Groq rate-limit headers and 429 semantics

Issue: #5. Researched 2026-10-28. Code under review:
`src/resilient_triage/graph/llm.py` → `GroqQuotaTracker.record_response`.

## Sources (primary)

- [S1] Groq docs, Rate Limits: https://console.groq.com/docs/rate-limits
- [S2] Groq docs, Errors: https://console.groq.com/docs/errors
- [S3] Official Groq Python SDK, `src/groq/_base_client.py`
  (`_parse_retry_after_header`, `_calculate_retry_timeout`, `_should_retry`):
  https://github.com/groq/groq-python/blob/main/src/groq/_base_client.py

## Header semantics (from S1)

| Header | Example | Window it describes | Format |
|---|---|---|---|
| `retry-after` | `2` | n/a | **Seconds**. Set **only on a 429** response |
| `x-ratelimit-limit-requests` | `14400` | **RPD**, requests per day | integer |
| `x-ratelimit-remaining-requests` | `14370` | **RPD** | integer |
| `x-ratelimit-reset-requests` | `2m59.56s` | **RPD** | duration string |
| `x-ratelimit-limit-tokens` | `18000` | **TPM**, tokens per minute | integer |
| `x-ratelimit-remaining-tokens` | `17997` | **TPM** | integer |
| `x-ratelimit-reset-tokens` | `7.66s` | **TPM** | duration string |

S1 says each `*-requests` header "Always refers to Requests Per Day (RPD)", and each
`*-tokens` header "Always refers to Tokens Per Minute (TPM)". The other limits, RPM and TPD
(and ASH/ASD for audio), are enforced but get **no headers**. Limits are per
**organization**, not per user or key (S1). S1 says its example values are illustrative.

### Duration-string format

S1 documents only the two examples above, `2m59.56s` and `7.66s`. They look like Go
`time.Duration.String()` output, with units `h`, `m`, `s`, `ms`, `µs`, `ns` and a
fractional final component (e.g. `1h2m3.5s`, `120ms`). **This is an inference, not
documented.** Groq publishes no grammar. A robust parser should accept any sequence of
`<number><unit>` with units `h|m|s|ms|us|µs|ns`, plus a bare number meaning seconds.

### 429 behaviour

- 429 Too Many Requests is returned when any limit is exceeded (S1). S2 only says
  "Implement request throttling and respect rate limits". It gives no backoff schedule and
  does not document an `error.code`. Don't rely on the body to tell which limit was hit.
- `retry-after` is present only on a 429 (S1). It is the one header tied to the 429 itself.
  The reset headers describe RPD/TPM only, so a 429 caused by **RPM or TPD** has no
  matching reset header.
- The official SDK (S3) on retry:
  1. reads `retry-after-ms` (non-standard), then `retry-after` as float seconds, then as
     an HTTP date;
  2. obeys it only if `0 < value <= 60` s, otherwise uses exponential backoff
     (0.5 s initial, 8 s max, jitter);
  3. obeys an `x-should-retry: true|false` header if one is present.

  The SDK **never** reads `x-ratelimit-reset-*` to decide when to retry.

## Does the current regex misparse? Yes

The current code reads `x-ratelimit-reset-requests` and runs
`re.search(r"(\d+(?:\.\d+)?)\s*s", value)`, with a 30 s default. Measured with that exact
regex:

| Header value | Parsed cooldown | Correct | Verdict |
|---|---|---|---|
| `7.66s` | 7.66 | 7.66 | ok |
| `0.5s` | 0.5 | 0.5 | ok |
| `2m59.56s` (S1's own example) | **59.56** | 179.56 | wrong: drops minutes |
| `1h2m3s` | **3** | 3723 | wrong: drops h/m |
| `120ms` | no match → 30 | 0.12 | wrong: falls to default |
| `59m` / `1h` | no match → 30 | 3540 / 3600 | wrong: falls to default |
| `2` (bare seconds) | no match → 30 | 2 | wrong: falls to default |

There is a second, bigger issue. **It reads the wrong header.** `x-ratelimit-reset-requests`
is the **daily** (RPD) window. On the free tier, a 429 is far more often caused by TPM or
RPM. In those cases this value has nothing to do with when the request may be retried. If
RPD is truly exhausted, the correct reset can be hours away. The parser would cap that at
the seconds component, or at the 30 s default, and keep retrying into a 429 wall.

## Recommendation: which header drives the 429 cooldown

1. **`retry-after` (seconds, float-tolerant) first.** It is the only header that S1 ties to
   the 429 response. The official SDK (S3) also uses it first.
2. If it is absent, look at the depleted bucket. If `remaining-tokens == 0`, use
   `reset-tokens`. If `remaining-requests == 0`, use `reset-requests`, which is the daily
   window: treat it as "primary exhausted for a long time", not a short cooldown. Parse both
   with a full duration parser.
3. Otherwise use the default (currently 30 s), bounded by config.
4. Keep the `reset-*` headers as quota telemetry, as `/llm/quota` already does, but label
   requests as **per-day** and tokens as **per-minute**.

## Implication for the "LLM gateway: interface and failure kinds" decision

- The gateway should report a typed **RateLimited** failure kind that carries
  `retry_after_s: float | None` and a `scope` of `minute | day | unknown`. A 429 is not one
  uniform "cooldown 30 s" event. A per-minute 429 means short cooldown and failover. A
  per-day exhaustion means long cooldown: stop probing the primary until the RPD reset.
- Header parsing (duration grammar, header precedence) belongs inside the Groq adapter, not
  in shared quota code. The interface only exposes seconds-to-wait plus scope.
- RPM and TPD have no headers. The gateway must handle a 429 with no matching reset header
  by falling back to `retry-after` or the bounded default.
- Optionally honour `x-should-retry` (S3) as a hint for retryable vs not.
