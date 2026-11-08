"""A remote vector store that never connects, so a semantic cache runs on its standby only.

A scenario run's private cache is built with it (ADR-009): the run never reaches the shared
Redis, and the cache's real connect-failure path puts it on the in-memory standby, which is
also how a scenario shows Redis down.
"""

from collections.abc import AsyncIterator

import numpy as np

from resilient_triage.cache.vector_store import (
    ServiceScope,
    StoreUnavailable,
    VectorEntry,
    VectorHit,
)

_REASON = "Redis unreachable (scenario run: private in-memory cache)"


class UnreachableRemoteStore:
    """RemoteVectorStore whose connect, and every other call, raises StoreUnavailable."""

    supports_search = False
    count = 0

    async def connect(self) -> None:
        """Fail to connect."""
        raise StoreUnavailable(_REASON)

    async def ping(self) -> None:
        """Fail the health check."""
        raise StoreUnavailable(_REASON)

    async def search(
        self, vector: np.ndarray, scope: ServiceScope, threshold: float
    ) -> VectorHit | None:
        """Fail: never reachable."""
        raise StoreUnavailable(_REASON)

    async def put(self, entry: VectorEntry, ttl: float) -> None:
        """Fail: never reachable."""
        raise StoreUnavailable(_REASON)

    async def delete(self, doc_id: str) -> None:
        """Fail: never reachable."""
        raise StoreUnavailable(_REASON)

    async def clear(self) -> None:
        """Fail: never reachable."""
        raise StoreUnavailable(_REASON)

    async def aclose(self) -> None:
        """Nothing to release."""

    async def entries(self) -> AsyncIterator[tuple[VectorEntry, float]]:
        """Fail: never reachable."""
        raise StoreUnavailable(_REASON)
        yield  # pragma: no cover - makes this an async generator
