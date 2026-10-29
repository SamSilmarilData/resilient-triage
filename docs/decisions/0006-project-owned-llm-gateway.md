# ADR-006: LLM Access Through a Project-Owned Gateway That Returns Failures as Values

## Status
Accepted (supersedes the Groq chat model and quota tracker design, including the fallback model, in ADR-005)

## Date
2026-10-28

## Context
All model access went through LangChain `BaseChatModel` subclasses (a Groq chat model and
a mock chat model for the simulation). Because of that shape:
- Every provider failure became an exception that `generate_report` caught with a bare
  `except Exception`. The graph then routed on a string-matched `"LLM Provider Failure"`
  and on `repair_attempts` being forced to its maximum.
- The sync and async paths were duplicated, and only the async one ran.
- Failover and the quota tracker were tangled together, so the fallback model's 429 put
  the primary into cooldown, and a 429 was retried against the primary.
- Provider discovery was copied in two places that disagreed.

## Decision
Every LLM call goes through one project-owned `LLMGateway` in `resilient_triage/llm/`:

- **Interface:** `async complete(messages) -> Completion | ProviderFailure`. The gateway
  never raises, except `CancelledError`.
- **Failure kinds:** `RateLimited(retry_after_s, scope)`, `Unavailable`, `Rejected` and
  `MalformedResponse`. Callers route on the kind, never on message text.
- **Model failover:** the gateway owns failover from the primary model to the fallback
  model and keeps a cooldown for each model. Adapters make exactly one call to one model.
- **Adapters:** `GroqAdapter` (HTTP; owns rate-limit header parsing) and `ScriptedAdapter`
  (test fake and zero-config simulation).

## Considered Options
- **Keep `BaseChatModel` and raise typed exceptions.** Rejected: exceptions are easy to
  swallow, and the LangChain surface brings the sync/async duplication with it.
- **Provider abstraction via `langchain_openai` / `langchain_google_genai`.** Rejected:
  neither package is a dependency, so those branches could never run.

## Consequences
- We lose LangChain callbacks and tracing hooks on model calls, and the ready-made
  provider integrations. A new provider means a new adapter.
- A text completion that isn't valid report JSON is not a provider failure. It stays the
  job of validation and self-repair (ADR-004).
- Tests script provider failures through `ScriptedAdapter` instead of mocking HTTP.
