"""VectorStore seam: the storage interface the semantic cache manager talks to."""

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class VectorEntry:
    """One cached report and the embedding of the scoped query that produced it."""

    doc_id: str
    query: str
    service_scope: str
    report_json: str
    vector: np.ndarray


@dataclass(frozen=True)
class VectorHit:
    """The best entry a search found at or above the threshold; the report is unvalidated."""

    doc_id: str
    query: str
    service_scope: str
    report_json: str
    similarity: float


class StoreUnavailable(Exception):
    """The store cannot be reached (connection-class failure); the manager fails over."""


class StoreQueryError(Exception):
    """The store is reachable but an operation failed; never a reason to disconnect."""


class VectorStore(Protocol):
    """Similarity search over cached entries, partitioned by service scope."""

    async def search(self, vector: np.ndarray, scope: str, threshold: float) -> VectorHit | None:
        """Return the most similar entry in `scope` with similarity >= threshold, else None."""
        ...

    async def put(self, entry: VectorEntry, ttl: float) -> None:
        """Store `entry` so it expires after `ttl` seconds."""
        ...

    async def clear(self) -> None:
        """Remove every entry."""
        ...

    @property
    def count(self) -> int:
        """Number of entries currently held."""
        ...
