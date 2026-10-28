# Research: pybreaker internals used by `AsyncCircuitBreaker`

Ticket: [#4](https://github.com/SamSilmarilData/resilient-triage/issues/4) (feeds [#10 Guarded telemetry probe: seam](https://github.com/SamSilmarilData/resilient-triage/issues/10)).
Date: 2026-10-28. Subject code: `src/resilient_triage/resilience/circuit_breaker.py`, `AsyncCircuitBreaker.call`.

## TL;DR

- **Installed:** pybreaker **1.4.1**, the latest PyPI release (2025-09-21). Upstream `master` has the same
  `__init__.py` as the 1.4.1 sdist. **Constraint:** `pybreaker>=1.2.0` in `pyproject.toml`, with no upper bound.
- **Internals:** every private name the adapter uses (`_lock`, `_state_storage.opened_at`, `half_open()`,
  `state._handle_error(exc, reraise=False)`, `state._handle_success()`, `listener.before_call`) is present
  with the same signature from **1.2.0 to 1.4.1**. Only `half_open()` and `before_call` are documented API.
- **The real break is in the declared floor.** pybreaker **1.2.0** stores `opened_at` as a *naive*
  datetime (`datetime.utcnow()`). The adapter compares it against `datetime.now(UTC)`. On 1.2.0 an open
  breaker therefore raises `TypeError: can't compare offset-naive and offset-aware datetimes` on every
  call, and the breaker stays OPEN forever. I reproduced this. Versions 1.3.0 and later are correct.
- **No supported asyncio path:** in 1.4.1 the only async API is `call_async` / `__pybreaker_call_async`,
  and both need **Tornado** (`tornado.gen.coroutine`). Native `acall()` exists only in *unmerged* upstream
  PRs #102 (closed) and #103 (open since 2026-03-21).
- **Behavioural gap (any version):** the adapter releases the lock across `await`, so in HALF_OPEN it lets
  **every concurrent caller through**, not one trial call. I reproduced 5 out of 5 probes executing.
- **Recommendation:** treat pybreaker as an implementation detail behind a project-owned async breaker
  interface. Tighten the constraint to `pybreaker>=1.3.0,<1.5`. Add a contract test that pins the internals.
  Add single-probe gating for half-open in the adapter. See the end of this file.

## 1. Version facts

| Source | Fact |
|---|---|
| `.venv/bin/pip show pybreaker` | `Version: 1.4.1` |
| `pyproject.toml:19` | `"pybreaker>=1.2.0"` (no upper bound, no lockfile in repo) |
| `docs/decisions/0003-…md:21` | ADR-003 says "Pybreaker (1.2+)" |
| [PyPI JSON](https://pypi.org/pypi/pybreaker/json) | Releases: 1.2.0 (2024-02-14), 1.3.0 (2025-03-05), 1.4.0 (2025-07-06), 1.4.1 (2025-09-21, latest) |
| [upstream commits](https://github.com/danielfm/pybreaker/commits/master) | Since 1.4.1 there are only a README fix (#106) and a test fix (#107), and `src/pybreaker/__init__.py` on master is byte-identical to the 1.4.1 sdist |

The installed `site-packages/pybreaker/__init__.py` is identical to the 1.4.1 sdist. I compared them with `diff`.
For every version in the allowed range I downloaded the sdist from PyPI and diffed it.

## 2. Each internal, across 1.2.0 → 1.4.1

Line numbers are for the [v1.4.1 source](https://github.com/danielfm/pybreaker/blob/v1.4.1/src/pybreaker/__init__.py).

| Adapter use | Defined at (1.4.1) | Public? | Changed 1.2.0→1.4.1? |
|---|---|---|---|
| `breaker._lock` (`with`) | L103 `self._lock = threading.RLock()` | private attr | No |
| `breaker._state_storage` | L104, set from the public `state_storage=` ctor arg | private attr | No |
| `.opened_at` | `CircuitBreakerStorage.opened_at` abstract property L408, memory L464, Redis L598 | public on the storage ABC | **Yes, 1.3.0.** It changed from a naive `datetime.utcnow()` (1.2.0 L257/L766) to an aware `datetime.now(UTC)`, and the Redis getter gained `tzinfo=UTC` (1.2.0 L523) |
| `breaker.half_open()` | L289 | **public, documented** | No |
| `breaker.state` | L179 property, which re-syncs from storage | public property, but returns state objects that are internal classes | No |
| `state._handle_error(exc, reraise=False)` | L689, with `Literal` overloads at L682/L686 | private | No (overloads already in 1.2.0) |
| `state._handle_success()` | L702 | private | Body unchanged. In 1.4.0 its callee `CircuitHalfOpenState.on_success` changed: it now increments a success counter and closes only at `success_threshold` (default 1, so behaviour is the same) |
| `listener.before_call(cb, func, *a, **kw)` | `CircuitBreakerListener.before_call` L650 | **public, documented** | No |

**What the adapter re-implements.** Its open-state check (circuit_breaker.py L159–169) copies
`CircuitOpenState.before_call` (L841–852) line for line, apart from the final re-entrant `self._breaker.call(...)`.
Its error and success branches copy `CircuitBreakerState.call` (L709–728). If upstream changes those
methods, the adapter will not pick the change up. This already happened once: the 1.3.0 UTC change was
not mirrored, and that produced the bug in §3.

**`_handle_error` can still raise when `reraise=False`.** It calls `on_failure`. On the tripping failure,
`CircuitClosedState.on_failure` (L807–815) and `CircuitHalfOpenState.on_failure` (L883–890) raise a
fresh `CircuitBreakerError`, because `throw_new_error_on_trip` defaults to True. The original exception
is then only available as `__context__`. The tests already allow for this with
`pytest.raises((RuntimeError, CircuitBreakerError))`.

## 3. Confirmed break: pybreaker 1.2.0 (inside the declared range)

1.2.0 source: `CircuitBreaker.open` sets `opened_at = datetime.utcnow()` (L257), which is naive. The adapter
then runs `datetime.now(UTC) < opened_at + timeout`, which compares an aware datetime with a naive one.

I reproduced it with a scratch script. The script loaded each sdist's `__init__.py` as `pybreaker`, imported
the real `AsyncCircuitBreaker`, tripped it with `fail_max=1, reset_timeout=0.1`, called it again, waited past
the timeout, then fired 5 concurrent calls:

```
== 1.2.0
trip -> CircuitBreakerError open
while open -> TypeError can't compare offset-naive and offset-aware datetimes
half-open concurrent probes executed: 0 results: ['TypeError'×5] open      # never recovers
== 1.3.0 / 1.4.1
trip -> CircuitBreakerError open
while open -> CircuitBreakerError Timeout not elapsed yet, circuit breaker still open
half-open concurrent probes executed: 5 results: ['str'×5] closed
```

Production impact on 1.2.0: `statuspage.py` / `chaos.py` catch `CircuitBreakerError` first and then fall
back to a generic `except Exception`. The request would still degrade and not return a 500. But the source
would be reported `UNAVAILABLE` instead of `CIRCUIT_OPEN`, and **the breaker could never half-open again**
until `/resilience/reset`.

## 4. Behavioural gap: half-open admits a flood

pybreaker's own `CircuitBreaker.call` (L246–251) holds the `RLock` for the entire guarded call. That
serialises calls, so in HALF_OPEN exactly one trial call runs. The adapter has to release the lock across
`await`, otherwise it would block the event loop. As a result, every coroutine that arrives while the
breaker is HALF_OPEN passes the gate and hits the recovering upstream. In the reproduction above, all 5
concurrent probes executed. pybreaker cannot express "one probe at a time" for asyncio. Any async adapter
needs its own in-flight flag for this.

(The RLock usage itself is sound. The adapter never awaits while holding it, so it does not block the loop.)

## 5. Is there a supported public async path in 1.4.1?

**No, not for asyncio.**

- `CircuitBreaker.call_async` (L266–279) and `CircuitBreakerState.call_async` (L730–756) are wrapped in
  `@gen.coroutine` from `tornado` (import at L44–49). The `__pybreaker_call_async=True` decorator flag
  raises `ImportError("No module named tornado")` without Tornado (L310–314). The upstream README lists only
  "Optional support for asynchronous Tornado calls" ([README.rst](https://github.com/danielfm/pybreaker/blob/master/README.rst),
  section "Optional Tornado Support").
- Tornado is not a project dependency, and adding it is out of bounds per CLAUDE.md. It would also hold a
  `threading.RLock` across a yield.
- Native asyncio: issue [#7 "Async support?"](https://github.com/danielfm/pybreaker/issues/7) and
  [#93](https://github.com/danielfm/pybreaker/issues/93) are open. PR
  [#103 "Add CircuitBreaker.acall for native asyncio"](https://github.com/danielfm/pybreaker/pull/103)
  (opened 2026-03-21, still open, with an "unreleased 1.5.0" changelog note) and
  [#104](https://github.com/danielfm/pybreaker/pull/104) are unmerged. #102 was closed unmerged. Nothing
  released offers this.

**Partly-public alternative within 1.4.1.** The adapter can avoid `_handle_error` / `_handle_success`
by reporting the outcome through the public `breaker.call()` with a sync thunk *after* the await:
`breaker.call(lambda: None)` on success, and `breaker.call(_raise, exc)` on failure. It can also avoid
`_state_storage` by building the `CircuitMemoryStorage` itself and passing it as `state_storage=`, which
keeps a reference. The costs: `listener.before_call` fires after the await instead of before (the
project's listener does not override `before_call`), and the open-state gate still has to be duplicated.
This removes the private-method dependency but adds a second, subtler coupling to the call semantics,
for little gain over a contract test.

## 6. Version-pinning risk (concrete)

| Risk | Likelihood | Effect |
|---|---|---|
| Fresh resolve picks **1.2.0** (e.g. someone pins down, or a constraints file) | low today, but the declared floor allows it | open breaker raises `TypeError` forever (§3) |
| A future **1.5.0** ships native `acall` (PR #103) and refactors `CircuitBreakerState.call` / `_handle_*` / adds an `asyncio.Lock` | medium, since a PR is open now and the floor has no upper bound | `AttributeError`/`TypeError` inside the adapter. Statuspage/chaos degrade to `UNAVAILABLE` on *every* call, so they are silently blind rather than returning a 500. The existing tests would catch it in CI, but only if CI resolves the new version |
| Upstream semantic change mirrored only in pybreaker's own `call` path (like the 1.3.0 UTC or 1.4.0 success-threshold changes) | medium | the adapter silently diverges from pybreaker's state machine |
| Upstream activity | low | 2 commits in the year since 1.4.1. The surface is stable, but it is not a maintained public contract |

## 7. Recommendation for #10 (Guarded telemetry probe: seam)

1. **Seam:** the guarded-probe module should depend on a **project-owned async breaker interface**
   (`async call(fn) -> T`, `info() -> CircuitBreakerInfo`, `reset()`), not on `pybreaker` types. Today
   `statuspage.py` / `chaos.py` import `pybreaker.CircuitBreakerError` directly. Re-export a project
   `CircuitOpenError` alias, or keep `CircuitBreakerError` but import it only through `resilience/`.
   This puts the pybreaker coupling in one adapter file.
2. **Keep pybreaker behind that adapter for now.** It is the only implementation, and swapping it is not
   justified. The internals are stable across 1.3–1.4.1.
3. **Fix the range:** `pybreaker>=1.3.0,<1.5` (and correct ADR-003's "1.2+"). This is not a new
   dependency, but it is a constraint change, so the spec should own it.
4. **Contract test:** add a test that asserts the internals exist with the expected shapes:
   `_lock`, an aware `_state_storage.opened_at` after `open()`, `state._handle_error` accepting
   `reraise=False`, and `state._handle_success`. A pybreaker bump then fails loudly in CI instead of
   degrading silently.
5. **Bug for the probe spec (regression test first):** add half-open single-probe gating. While HALF_OPEN,
   allow one in-flight call and fail the rest fast with `CircuitBreakerError`. Also stop duplicating the
   open-state timeout check by deriving it from one helper.
6. **Revisit** when pybreaker ships native `acall` (watch PR #103). Then the adapter can collapse to
   delegation and drop every private access.

## Sources

- Installed source: `.venv/lib/python3.12/site-packages/pybreaker/__init__.py` (1.4.1)
- PyPI sdists 1.0.0, 1.1.0, 1.2.0, 1.3.0, 1.4.0, 1.4.1 via https://pypi.org/pypi/pybreaker/json, diffed locally
- https://github.com/danielfm/pybreaker/blob/v1.4.1/src/pybreaker/__init__.py
- https://github.com/danielfm/pybreaker/compare/1.2.0...v1.3.0 (UTC change), https://github.com/danielfm/pybreaker/compare/v1.3.0...v1.4.0 (success_threshold)
- https://github.com/danielfm/pybreaker/blob/master/README.rst ("Optional Tornado Support")
- https://github.com/danielfm/pybreaker/issues/7, /issues/93, /pull/102, /pull/103, /pull/104
