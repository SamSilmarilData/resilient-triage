"""VectorStore seam: the storage interface the semantic cache manager talks to."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import NewType, Protocol

import numpy as np

# A normalize_scope()'d service scope; "" is the "no scope" scope, matching only itself.
ServiceScope = NewType("ServiceScope", str)


@dataclass(frozen=True)
class VectorEntry:
    """One cached report and the embedding of the scoped query that produced it."""

    doc_id: str
    query: str
    service_scope: ServiceScope
    report_json: str
    vector: np.ndarray


@dataclass(frozen=True)
class VectorHit:
    """The best entry a search found at or above the threshold; its report is unvalidated."""

    entry: VectorEntry
    similarity: float


class StoreUnavailable(Exception):
    """The store cannot be reached (connection-class failure); the manager fails over."""


class StoreQueryError(Exception):
    """The store is reachable but an operation failed; never a reason to disconnect."""


class VectorStore(Protocol):
    """Similarity search over cached entries, partitioned by service scope."""

    async def search(
        self, vector: np.ndarray, scope: ServiceScope, threshold: float
    ) -> VectorHit | None:
        """Return the most similar entry in `scope` with similarity >= threshold, else None."""
        ...

    async def put(self, entry: VectorEntry, ttl: float) -> None:
        """Store `entry` so it expires after `ttl` seconds."""
        ...

    async def delete(self, doc_id: str) -> None:
        """Remove the entry stored under `doc_id`; an unknown id is a no-op."""
        ...

    async def clear(self) -> None:
        """Remove every entry."""
        ...

    @property
    def count(self) -> int:
        """Entries written or loaded by this process and not since removed, ignoring expiry."""
        ...


class RemoteVectorStore(VectorStore, Protocol):
    """A VectorStore behind a connection (Redis): connectable, and maybe search-incapable.

    Every method may raise StoreUnavailable (unreachable) or StoreQueryError (rejected).
    """

    @property
    def supports_search(self) -> bool:
        """Whether `search` works; False means reads must come from a hydrated standby."""
        ...

    async def connect(self) -> None:
        """Open the connection and detect capabilities."""
        ...

    async def ping(self) -> None:
        """Round-trip a health check."""
        ...

    async def aclose(self) -> None:
        """Release the connection pool; never raises."""
        ...

    def entries(self) -> AsyncIterator[tuple[VectorEntry, float]]:
        """Yield every stored entry with its remaining TTL in seconds."""
        ...
