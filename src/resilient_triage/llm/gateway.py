"""LLM gateway: the one seam every model call goes through, returning failures as values."""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from langchain_core.messages import BaseMessage

from resilient_triage.config import settings
from resilient_triage.schemas.api import LLMQuotaSnapshot

logger = logging.getLogger(__name__)

# Today's failover behaviour, moved unchanged: the primary gets two attempts on a transient
# failure, and a rate limit without a usable reset header cools the primary down for 30s.
PRIMARY_ATTEMPTS = 2
DEFAULT_COOLDOWN_S = 30.0


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


class ModelAdapter(Protocol):
    """One call to one model; never retries or fails over (the gateway does that)."""

    async def call(self, model: str, messages: list[BaseMessage]) -> Completion | ProviderFailure:
        """Send messages to model once and return its completion or a failure kind."""
        ...

    async def aclose(self) -> None:
        """Release whatever the adapter owns."""
        ...


@dataclass(frozen=True)
class QuotaHeaders:
    """Rate-limit header values and HTTP status from the provider's latest responses."""

    remaining_requests: int | None = None
    limit_requests: int | None = None
    remaining_tokens: int | None = None
    limit_tokens: int | None = None
    reset_requests: str | None = None
    reset_tokens: str | None = None
    last_status: int | None = None


@runtime_checkable
class QuotaReporter(Protocol):
    """An adapter that reads rate-limit headers and reports them to the gateway's snapshot."""

    def quota_headers(self) -> QuotaHeaders:
        """Return the latest header values seen."""
        ...


class LLMGateway:
    """Calls the primary model through an adapter, failing over to the fallback model."""

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
        self._active_model = primary
        self._total_calls = 0
        self._rate_limit_hits = 0
        self._failover_events = 0
        self._last_error: str | None = None
        self._last_updated: datetime | None = None
        self._cooldown_until = 0.0
        self._clock = clock

    @property
    def label(self) -> str:
        """The display name of the active provider (the /health "active_model")."""
        return self._label

    async def complete(self, messages: list[BaseMessage]) -> Completion | ProviderFailure:
        """Return a completion or a provider failure; raises nothing but CancelledError."""
        try:
            return await self._complete(messages)
        except Exception as exc:
            logger.error("LLM gateway call failed unexpectedly: %s", exc)
            return Unavailable(detail=f"LLM gateway error: {exc}")

    async def _complete(self, messages: list[BaseMessage]) -> Completion | ProviderFailure:
        if self._clock() < self._cooldown_until:
            return await self._call(self.fallback, messages, failover=True)
        failure: ProviderFailure | None = None
        for attempt in range(PRIMARY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(
                    random.uniform(settings.retry_min_wait_seconds, settings.retry_max_wait_seconds)
                )
            result = await self._call(self.primary, messages, failover=False)
            if isinstance(result, RateLimited):
                logger.warning(
                    "LLM rate limit hit for %s; failing over to %s", self.primary, self.fallback
                )
                fallback_result = await self._call(self.fallback, messages, failover=True)
                if isinstance(fallback_result, Completion):
                    return fallback_result
                failure = result
            elif isinstance(result, Unavailable):
                failure = result
            else:
                return result
        assert failure is not None
        return failure

    async def _call(
        self, model: str, messages: list[BaseMessage], *, failover: bool
    ) -> Completion | ProviderFailure:
        result = await self.adapter.call(model, messages)
        self._total_calls += 1
        self._active_model = model
        self._last_updated = datetime.now(UTC)
        if isinstance(result, Completion):
            if failover:
                self._failover_events += 1
            return result
        self._last_error = result.detail
        logger.warning("LLM call to %s failed: %s", model, result)
        if isinstance(result, RateLimited):
            self._rate_limit_hits += 1
            cooldown = result.retry_after_s or DEFAULT_COOLDOWN_S
            self._cooldown_until = self._clock() + cooldown
        return result

    def snapshot(self) -> LLMQuotaSnapshot:
        """Return the quota, counters and last call of this gateway (the /llm/quota body)."""
        headers = (
            self.adapter.quota_headers()
            if isinstance(self.adapter, QuotaReporter)
            else QuotaHeaders()
        )
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
        self._cooldown_until = 0.0

    async def aclose(self) -> None:
        """Close the adapter; never raises."""
        try:
            await self.adapter.aclose()
        except Exception as exc:
            logger.warning("LLM adapter close failed: %s", exc)
