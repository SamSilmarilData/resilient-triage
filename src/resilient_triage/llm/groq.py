"""GroqAdapter: one HTTP call to one Groq model, mapped to a Completion or a failure kind."""

import logging
import re

import httpx
from langchain_core.messages import BaseMessage

from resilient_triage.llm.gateway import (
    Completion,
    MalformedResponse,
    ProviderFailure,
    QuotaHeaders,
    RateLimited,
    Rejected,
    Unavailable,
)

logger = logging.getLogger(__name__)

GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"


def new_groq_http_client(timeout: float) -> httpx.AsyncClient:
    """Build a keep-alive pooled httpx.AsyncClient for Groq LPU calls (no I/O until used)."""
    return httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    )


def format_messages(messages: list[BaseMessage]) -> list[dict[str, str]]:
    """Map LangChain messages onto OpenAI-style chat roles."""
    roles = {"human": "user", "ai": "assistant", "system": "system"}
    return [{"role": roles.get(m.type, "user"), "content": str(m.content)} for m in messages]


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", resp.text))
    except Exception:
        return resp.text


def _int_header(headers: httpx.Headers, name: str, current: int | None) -> int | None:
    """The header as an int, else the value already known."""
    try:
        return int(headers[name]) if name in headers else current
    except ValueError:
        return current


def _rate_limit_cooldown_s(resp: httpx.Response) -> float | None:
    """Seconds from the requests-bucket reset header, as parsed before the gateway existed."""
    reset_header = resp.headers.get("x-ratelimit-reset-requests")
    if reset_header:
        match = re.search(r"(\d+(?:\.\d+)?)\s*s", reset_header)
        if match:
            return float(match.group(1))
    return None


class GroqAdapter:
    """Calls Groq's chat completions API through an HTTP client it owns."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = http_client if http_client is not None else new_groq_http_client(timeout_s)
        self._headers: dict[str, QuotaHeaders] = {}

    def quota_headers(self, model: str) -> QuotaHeaders:
        """Return the rate-limit headers and status of model's latest responses."""
        return self._headers.get(model, QuotaHeaders())

    async def call(self, model: str, messages: list[BaseMessage]) -> Completion | ProviderFailure:
        """POST messages to model once; never raises except CancelledError."""
        payload = {
            "model": model,
            "messages": format_messages(messages),
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 2500,
        }
        try:
            resp = await self._client.post(
                GROQ_CHAT_COMPLETIONS_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.TimeoutException as exc:
            return Unavailable(detail=f"Groq request to {model} timed out: {exc!r}")
        except httpx.HTTPError as exc:
            return Unavailable(detail=f"Groq request to {model} failed: {exc!r}")
        self._record_headers(model, resp)
        return self._to_result(resp, model)

    def _record_headers(self, model: str, resp: httpx.Response) -> None:
        known, headers = self.quota_headers(model), resp.headers
        self._headers[model] = QuotaHeaders(
            remaining_requests=_int_header(
                headers, "x-ratelimit-remaining-requests", known.remaining_requests
            ),
            limit_requests=_int_header(headers, "x-ratelimit-limit-requests", known.limit_requests),
            remaining_tokens=_int_header(
                headers, "x-ratelimit-remaining-tokens", known.remaining_tokens
            ),
            limit_tokens=_int_header(headers, "x-ratelimit-limit-tokens", known.limit_tokens),
            reset_requests=headers.get("x-ratelimit-reset-requests", known.reset_requests),
            reset_tokens=headers.get("x-ratelimit-reset-tokens", known.reset_tokens),
            last_status=resp.status_code,
        )

    def _to_result(self, resp: httpx.Response, model: str) -> Completion | ProviderFailure:
        status = resp.status_code
        if status == 429:
            return RateLimited(
                retry_after_s=_rate_limit_cooldown_s(resp),
                scope="unknown",
                detail=_error_message(resp),
            )
        if status >= 500:
            return Unavailable(detail=f"HTTP {status}: {_error_message(resp)}")
        if status >= 400:
            return Rejected(detail=f"HTTP {status}: {_error_message(resp)}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return MalformedResponse(detail=f"HTTP {status} without message content: {exc!r}")
        if not isinstance(content, str):
            return MalformedResponse(detail=f"HTTP {status} with non-text content")
        return Completion(text=content, model=model)

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
