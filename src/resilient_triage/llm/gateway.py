"""LLM gateway: the one seam every model call goes through, returning failures as values."""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from langchain_core.messages import BaseMessage

from resilient_triage.config import settings
from resilient_triage.schemas.api import LLMQuotaSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Completion:
    """Text a model returned, and the model that returned it."""

    text: str
    model: str


@dataclass(frozen=True)
class RateLimited:
    """The provider refused the call for quota (HTTP 429)."""

    retry_after_s: float | None
    scope: Literal["minute", "day", "unknown"]
    detail: str


@dataclass(frozen=True)
class Unavailable:
    """The provider could not answer: 5xx, timeout or connection error."""

    detail: str


@dataclass(frozen=True)
class Rejected:
    """The provider refused the request itself: a 4xx other than 429."""

    detail: str


@dataclass(frozen=True)
class MalformedResponse:
    """A 2xx answer without choices[0].message.content."""

    detail: str


ProviderFailure = RateLimited | Unavailable | Rejected | MalformedResponse


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


# One adapter call made during a complete(): the model called and what it returned.
Attempt = tuple[str, Completion | ProviderFailure]


class LLMGateway:
    """Calls the primary model through an adapter, failing over to the fallback model.

    At most 3 adapter calls per complete(). The primary's result decides what follows:
    RateLimited cools the primary down and calls the fallback; Unavailable retries the primary
    once after a jittered backoff, then calls the fallback; MalformedResponse calls the
    fallback; Rejected returns at once. While the primary cools down, only the fallback is
    called. Cooldowns are per model.
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

    async def complete(self, messages: list[BaseMessage]) -> Completion | ProviderFailure:
        """Return a completion or a provider failure; raises nothing but CancelledError."""
        attempts: list[Attempt] = []
        result: Completion | ProviderFailure
        try:
            result = await self._complete(messages, attempts)
        except Exception as exc:
            logger.error("LLM gateway call failed unexpectedly: %s", exc)
            result = Unavailable(detail=f"LLM gateway error: {exc}")
        # Reached only when not cancelled: a cancelled call records nothing.
        self._record(attempts, result)
        return result

    async def _complete(
        self, messages: list[BaseMessage], attempts: list[Attempt]
    ) -> Completion | ProviderFailure:
        if self._cooling_down(self.primary):
            return await self._call(self.fallback, messages, attempts)
        result = await self._call(self.primary, messages, attempts)
        if isinstance(result, Unavailable):
            await asyncio.sleep(
                random.uniform(settings.retry_min_wait_seconds, settings.retry_max_wait_seconds)
            )
            result = await self._call(self.primary, messages, attempts)
        if isinstance(result, Completion | Rejected):
            return result
        logger.warning("LLM primary %s failed; failing over to %s", self.primary, self.fallback)
        return await self._call(self.fallback, messages, attempts)

    def _cooling_down(self, model: str) -> bool:
        return self._clock() < self._cooldown_until.get(model, 0.0)

    async def _call(
        self, model: str, messages: list[BaseMessage], attempts: list[Attempt]
    ) -> Completion | ProviderFailure:
        result = await self.adapter.call(model, messages)
        attempts.append((model, result))
        if not isinstance(result, Completion):
            logger.warning("LLM call to %s failed: %s", model, result)
        return result

    def _record(self, attempts: list[Attempt], result: Completion | ProviderFailure) -> None:
        """Apply one completed complete() call to the counters and cooldowns."""
        now = self._clock()
        for model, outcome in attempts:
            self._total_calls += 1
            self._last_updated = datetime.now(UTC)
            if isinstance(outcome, Completion):
                self._active_model = model
                continue
            self._last_error = outcome.detail
            if isinstance(outcome, RateLimited):
                self._rate_limit_hits += 1
                self._cooldown_until[model] = now + self._cooldown_s(model, outcome)
        if isinstance(result, Completion) and attempts and attempts[-1][0] == self.fallback:
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
