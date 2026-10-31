"""LLM gateway: the one seam every model call goes through, returning failures as values."""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from langchain_core.messages import BaseMessage

from resilient_triage.config import settings
from resilient_triage.llm.failures import (
    ProviderFailure,
    RateLimited,
    Rejected,
    Unavailable,
)
from resilient_triage.schemas.api import LLMQuotaSnapshot
from resilient_triage.trace.recorder import NullTrace, TraceRecorder, record_event

if TYPE_CHECKING:
    from resilient_triage.trace.fields import ModelRole

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Completion:
    """Text a model returned, and the model that returned it."""

    text: str
    model: str


@dataclass(frozen=True)
class QuotaHeaders:
    """Rate-limit header values and HTTP status from one model's latest responses."""

    remaining_requests: int | None = None
    limit_requests: int | None = None
    remaining_tokens: int | None = None
    limit_tokens: int | None = None
    reset_requests: str | None = None
    reset_tokens: str | None = None
    last_status: int | None = None
    # The two reset headers in seconds, which size a rate-limit cooldown by scope.
    reset_requests_s: float | None = None
    reset_tokens_s: float | None = None


class ModelAdapter(Protocol):
    """One call to one model; never retries or fails over (the gateway does that)."""

    def label(self, primary: str) -> str:
        """Return the /health label for this adapter serving the primary model."""
        ...

    def quota_headers(self, model: str) -> QuotaHeaders:
        """Return the latest rate-limit header values seen for model (empty if none)."""
        ...

    async def call(self, model: str, messages: list[BaseMessage]) -> Completion | ProviderFailure:
        """Send messages to model once and return its completion or a failure kind."""
        ...

    async def aclose(self) -> None:
        """Release whatever the adapter owns."""
        ...


@dataclass(frozen=True)
class Attempt:
    """One adapter call made during a complete(): the model called and what it returned."""

    model: str
    result: Completion | ProviderFailure


class LLMGateway:
    """Calls the primary model through an adapter, failing over to the fallback model.

    At most 3 adapter calls per complete(). The primary's result decides what follows:
    RateLimited cools the primary down and calls the fallback; Unavailable retries the primary
    once after a jittered backoff, then calls the fallback; MalformedResponse calls the
    fallback; Rejected returns at once.

    Cooldowns are per model, and a model in cooldown is never called. While the primary cools
    down, only the fallback is called; while the fallback cools down, a primary failure is
    returned as is; while both cool down, no call is made and a RateLimited carrying the
    shorter remaining window is returned. A 429 starts its model's cooldown when it arrives,
    so concurrent and later-cancelled calls see it; counters are applied only when complete()
    finishes. Known limits: calls already waiting on the primary when its first 429 arrives
    still get their own 429s (no single-flight), and the Unavailable retry is sent even if a
    concurrent call put the primary into cooldown during the backoff.
    """

    def __init__(
        self,
        adapter: ModelAdapter,
        primary: str,
        fallback: str,
        label: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self.primary = primary
        self.fallback = fallback
        self._label = label
        self._clock = clock
        self._active_model = primary
        self._total_calls = 0
        self._rate_limit_hits = 0
        self._failover_events = 0
        self._last_error: str | None = None
        self._last_updated: datetime | None = None
        self._cooldown_until: dict[str, float] = {}

    @property
    def label(self) -> str:
        """The display name of the active provider (the /health "active_model")."""
        return self._label

    async def complete(
        self, messages: list[BaseMessage], *, trace: TraceRecorder | None = None
    ) -> Completion | ProviderFailure:
        """Return a completion or a provider failure; raises nothing but CancelledError.

        Each adapter attempt is recorded on trace as one `model_call` (ADR-008); with no
        trace (the default, e.g. for /chaos/*), nothing is recorded.
        """
        attempts: list[Attempt] = []
        result: Completion | ProviderFailure
        try:
            result = await self._complete(messages, attempts, trace or NullTrace())
        except Exception as exc:
            logger.error("LLM gateway call failed unexpectedly: %s", exc)
            result = Unavailable(detail=f"LLM gateway error: {exc}")
        # Reached only when not cancelled: a cancelled call records no counters (a 429 it got
        # has already started its cooldown in _call).
        self._record(attempts, result)
        return result

    async def _complete(
        self, messages: list[BaseMessage], attempts: list[Attempt], trace: TraceRecorder
    ) -> Completion | ProviderFailure:
        primary_left_s = self._remaining_s(self.primary)
        if primary_left_s > 0:
            fallback_left_s = self._remaining_s(self.fallback)
            if fallback_left_s > 0:
                # No call made and no 429 received, so attempts stays empty: nothing is recorded.
                # Reusing the values checked above keeps retry_after_s positive.
                return RateLimited(
                    retry_after_s=min(primary_left_s, fallback_left_s),
                    scope="unknown",
                    detail=f"Both {self.primary} and {self.fallback} are cooling down",
                )
            return await self._call(self.fallback, messages, attempts, trace)
        result = await self._call(self.primary, messages, attempts, trace)
        if isinstance(result, Unavailable):
            await asyncio.sleep(
                random.uniform(settings.retry_min_wait_seconds, settings.retry_max_wait_seconds)
            )
            result = await self._call(self.primary, messages, attempts, trace)
        if isinstance(result, Completion | Rejected):
            return result
        if self._cooling_down(self.fallback):
            logger.warning(
                "LLM primary %s failed; fallback %s is cooling down", self.primary, self.fallback
            )
            return result
        logger.warning("LLM primary %s failed; failing over to %s", self.primary, self.fallback)
        return await self._call(self.fallback, messages, attempts, trace)

    def _cooling_down(self, model: str) -> bool:
        return self._remaining_s(model) > 0

    def _remaining_s(self, model: str) -> float:
        return self._cooldown_until.get(model, 0.0) - self._clock()

    async def _call(
        self,
        model: str,
        messages: list[BaseMessage],
        attempts: list[Attempt],
        trace: TraceRecorder,
    ) -> Completion | ProviderFailure:
        started = time.monotonic()
        try:
            result = await self.adapter.call(model, messages)
        except Exception as exc:
            # complete() answers this with Unavailable: trace the attempt as one.
            self._trace_call(trace, model, Unavailable(detail=str(exc)), started)
            raise
        self._trace_call(trace, model, result, started)
        attempts.append(Attempt(model=model, result=result))
        if isinstance(result, RateLimited):
            # Started here, not in _record, so concurrent and cancelled calls see it at once.
            # The later deadline wins: a 429 never shortens a running cooldown.
            until = self._clock() + self._cooldown_s(model, result)
            self._cooldown_until[model] = max(self._cooldown_until.get(model, 0.0), until)
        if not isinstance(result, Completion):
            logger.warning("LLM call to %s failed: %s", model, result)
        return result

    def _trace_call(
        self,
        trace: TraceRecorder,
        model: str,
        result: Completion | ProviderFailure,
        started: float,
    ) -> None:
        """Record one adapter attempt as a model_call: its outcome kind, never its text."""
        # Imported here: trace.events imports schemas.api, which imports this package.
        from resilient_triage.trace.events import ModelCall

        duration_ms = round((time.monotonic() - started) * 1000, 3)
        role: ModelRole = "fallback" if model == self.fallback else "primary"
        record_event(
            trace,
            lambda: ModelCall.from_result(
                model=model, role=role, result=result, duration_ms=duration_ms
            ),
        )

    def _record(self, attempts: list[Attempt], result: Completion | ProviderFailure) -> None:
        """Apply one completed complete() call to the counters (cooldowns start in _call)."""
        for attempt in attempts:
            model, outcome = attempt.model, attempt.result
            self._total_calls += 1
            self._last_updated = datetime.now(UTC)
            if isinstance(outcome, Completion):
                self._active_model = model
                continue
            self._last_error = outcome.detail
            if isinstance(outcome, RateLimited):
                self._rate_limit_hits += 1
        if isinstance(result, Completion) and attempts and attempts[-1].model == self.fallback:
            self._failover_events += 1

    def _cooldown_s(self, model: str, limited: RateLimited) -> float:
        """Seconds model cools down: minute and day use their bucket's reset, capped."""
        seconds: float | None = None
        if limited.scope == "minute":
            seconds = limited.retry_after_s
            if seconds is None:
                seconds = self.adapter.quota_headers(model).reset_tokens_s
        elif limited.scope == "day":
            seconds = self.adapter.quota_headers(model).reset_requests_s
            if seconds is None:
                seconds = limited.retry_after_s
        if seconds is None:
            seconds = settings.llm_default_cooldown_s
        return min(seconds, settings.llm_max_cooldown_s)

    def snapshot(self) -> LLMQuotaSnapshot:
        """Return the quota, counters and last call of this gateway (the /llm/quota body)."""
        headers = self.adapter.quota_headers(self.primary)
        return LLMQuotaSnapshot(
            active_model=self._active_model,
            fallback_model=self.fallback,
            remaining_requests=headers.remaining_requests,
            limit_requests=headers.limit_requests,
            remaining_tokens=headers.remaining_tokens,
            limit_tokens=headers.limit_tokens,
            reset_requests=headers.reset_requests,
            reset_tokens=headers.reset_tokens,
            total_calls=self._total_calls,
            rate_limit_hits=self._rate_limit_hits,
            failover_events=self._failover_events,
            last_status=headers.last_status,
            last_error=self._last_error,
            last_updated=self._last_updated,
        )

    def reset(self) -> None:
        """Zero the counters, clear the last error and end any cooldown."""
        self._total_calls = 0
        self._rate_limit_hits = 0
        self._failover_events = 0
        self._last_error = None
        self._cooldown_until.clear()

    async def aclose(self) -> None:
        """Close the adapter; never raises."""
        try:
            await self.adapter.aclose()
        except Exception as exc:
            logger.warning("LLM adapter close failed: %s", exc)
