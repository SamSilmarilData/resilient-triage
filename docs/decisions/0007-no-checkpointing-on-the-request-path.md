# ADR-007: No Checkpointing on the Request Path

## Status
Accepted

## Date
2026-10-28

## Context
The triage graph was compiled with `BoundedMemorySaver`, and `POST /triage` ran every request
on a fresh `{X-Thread-ID}_{random}` thread. No request ever resumed a thread, so the
checkpointer bought no multi-turn memory. It did cost a write per superstep and leaked one
thread per request: `BoundedMemorySaver` never evicts under `ainvoke`, and when it does evict it
leaves `writes` / `blobs` behind (see "LangGraph MemorySaver internals used by
BoundedMemorySaver", issue #6).

## Decision
The request-path graph is compiled **without a checkpointer**, and `BoundedMemorySaver` is
deleted. Each run is self-contained: its state lives only for the duration of `ainvoke`.
`X-Thread-ID` stays as a header the caller sets and gets echoed back, for correlation only.

## Considered Options
- **Delete each thread after its run (`adelete_thread`).** Rejected: it keeps paying for
  checkpoints that are never read, only to throw them away.
- **Fix `BoundedMemorySaver` to evict through the public API.** Rejected: it bounds a cache of
  state that nothing reads.

## Consequences
- Multi-turn triage (a follow-up question that sees the previous run) is not supported. Adding
  it later means choosing a durable checkpointer and a thread-resume contract on purpose, not
  re-enabling this one.
- `X-Thread-ID` no longer names a checkpoint; `docs/api.md` describes it as a correlation id.
