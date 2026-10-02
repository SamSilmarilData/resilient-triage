"""Tenacity retry policies with exponential backoff and randomized jitter."""

import functools
import logging
from typing import Any, Awaitable, Callable, TypeVar

import httpx
import tenacity

from resilient_triage.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


def is_transient_error(exc: BaseException) -> bool:
    """Determine whether an exception represents a transient failure eligible for retry.

    Retries on:
      - httpx.TimeoutException (ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout)
      - httpx.NetworkError (ConnectError, RemoteProtocolError)
      - httpx.HTTPStatusError with status 429 (rate limit) or 5xx (server error)

    Does NOT retry on client errors (400, 401, 403, 404) or business logic/validation errors.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_HTTP_STATUS_CODES

    return False


def _log_retry_attempt(retry_state: tenacity.RetryCallState) -> None:
    """Log retry attempts with backoff delay and exception context."""
    attempt_num = retry_state.attempt_number
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    next_action = retry_state.next_action
    sleep_seconds = next_action.sleep if next_action else 0.0

    logger.warning(
        "Transient error encountered on attempt %d: %s. Retrying in %.3fs...",
        attempt_num,
        exc,
        sleep_seconds,
    )


def with_retry(
    max_attempts: int | None = None,
    min_wait: float | None = None,
    max_wait: float | None = None,
    multiplier: float = 0.1,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator applying Tenacity exponential backoff with randomized jitter to async functions.

    Defaults are loaded from application settings.
    """
    effective_max_attempts = max_attempts if max_attempts is not None else settings.retry_max_attempts
    effective_min_wait = min_wait if min_wait is not None else settings.retry_min_wait_seconds
    effective_max_wait = max_wait if max_wait is not None else settings.retry_max_wait_seconds

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            retrying = tenacity.AsyncRetrying(
                stop=tenacity.stop_after_attempt(effective_max_attempts),
                wait=tenacity.wait_random_exponential(
                    multiplier=multiplier,
                    min=effective_min_wait,
                    max=effective_max_wait,
                ),
                retry=tenacity.retry_if_exception(is_transient_error),
                before_sleep=_log_retry_attempt,
                reraise=True,
            )
            async for attempt in retrying:
                with attempt:
                    return await func(*args, **kwargs)
            # Unreachable with reraise=True, but satisfies type checker
            raise RuntimeError("Retries exhausted without return")

        return wrapper

    return decorator


async def execute_with_retry(
    coro_func: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int | None = None,
    min_wait: float | None = None,
    max_wait: float | None = None,
    multiplier: float = 0.1,
    **kwargs: Any,
) -> T:
    """Execute an async callable directly with Tenacity exponential backoff and jitter."""
    decorated = with_retry(
        max_attempts=max_attempts,
        min_wait=min_wait,
        max_wait=max_wait,
        multiplier=multiplier,
    )(coro_func)
    return await decorated(*args, **kwargs)
