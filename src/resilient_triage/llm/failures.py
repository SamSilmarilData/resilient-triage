"""Provider failure kinds the LLM gateway returns as values; no imports, so state can use them."""

from dataclasses import dataclass
from typing import Literal

# Which quota a 429 hit: the per-minute bucket, the per-day bucket, or not known.
RateLimitScope = Literal["minute", "day", "unknown"]


@dataclass(frozen=True)
class RateLimited:
    """The provider refused the call for quota (HTTP 429)."""

    retry_after_s: float | None
    scope: RateLimitScope
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
