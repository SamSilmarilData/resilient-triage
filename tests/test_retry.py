"""Tests for Tenacity retry decorator and transient error classification."""

import time
import httpx
import pytest

from resilient_triage.resilience.retry import (
    execute_with_retry,
    is_transient_error,
    with_retry,
)


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.com/test")
    resp = httpx.Response(status_code, request=req)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=req, response=resp)


def test_is_transient_error():
    """Verify that only transient exceptions are flagged for retry."""
    req = httpx.Request("GET", "https://example.com/test")

    # Timeouts and network errors -> Transient
    assert is_transient_error(httpx.ReadTimeout("Timeout", request=req))
    assert is_transient_error(httpx.ConnectError("Connection refused"))

    # 429 and 5xx -> Transient
    assert is_transient_error(_make_http_status_error(429))
    assert is_transient_error(_make_http_status_error(500))
    assert is_transient_error(_make_http_status_error(502))
    assert is_transient_error(_make_http_status_error(503))
    assert is_transient_error(_make_http_status_error(504))

    # Client errors -> NOT Transient (Fail Fast)
    assert not is_transient_error(_make_http_status_error(400))
    assert not is_transient_error(_make_http_status_error(401))
    assert not is_transient_error(_make_http_status_error(403))
    assert not is_transient_error(_make_http_status_error(404))

    # Generic exceptions -> NOT Transient
    assert not is_transient_error(ValueError("Invalid argument"))
    assert not is_transient_error(KeyError("missing key"))


@pytest.mark.asyncio
async def test_retry_on_transient_http_429():
    """Verify that a transient 429 rate limit is retried and recovers."""
    attempts = 0

    @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.05)
    async def flaky_call():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _make_http_status_error(429)
        return "success"

    result = await flaky_call()
    assert result == "success"
    assert attempts == 3


@pytest.mark.asyncio
async def test_retry_exhaustion_raises_original_error():
    """Verify that when max retry attempts are exhausted, the original error is raised."""
    attempts = 0

    @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.05)
    async def always_failing():
        nonlocal attempts
        attempts += 1
        raise _make_http_status_error(503)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await always_failing()

    assert exc_info.value.response.status_code == 503
    assert attempts == 3


@pytest.mark.asyncio
async def test_no_retry_on_client_error():
    """Verify that non-transient client errors fail immediately on attempt 1."""
    attempts = 0

    @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.05)
    async def bad_request_call():
        nonlocal attempts
        attempts += 1
        raise _make_http_status_error(400)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await bad_request_call()

    assert exc_info.value.response.status_code == 400
    assert attempts == 1


@pytest.mark.asyncio
async def test_execute_with_retry_helper():
    """Verify direct functional execution with execute_with_retry."""
    attempts = 0

    async def flaky_network():
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise httpx.ConnectError("Network blip")
        return {"status": "ok"}

    result = await execute_with_retry(
        flaky_network,
        max_attempts=3,
        min_wait=0.01,
        max_wait=0.05,
    )
    assert result == {"status": "ok"}
    assert attempts == 2
