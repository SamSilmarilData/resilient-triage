"""InMemoryVectorStore: NumPy cosine search with TTL purge and oldest-first capacity eviction."""

import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from resilient_triage.cache.vector_store import VectorEntry, VectorHit


@dataclass
class _Stored:
    entry: VectorEntry
    expires_at: float


class InMemoryVectorStore:
    """Process-local VectorStore; vectors are L2-normalised, so a dot product is cosine."""

    def __init__(
        self,
        max_capacity: int = 10000,
        clock: Callable[[], float] = time.monotonic,
        dimension: int | None = None,
    ) -> None:
        self.max_capacity = max_capacity
        self.dimension = dimension
        self._clock = clock
        self._docs: dict[str, _Stored] = {}

    async def search(self, vector: np.ndarray, scope: str, threshold: float) -> VectorHit | None:
        """Return the most similar live entry in `scope` at or above `threshold`."""
        if not self._fits(vector):
            return None
        self._purge_expired()
        # "No scope" is a scope of its own: "" matches only entries stored without one.
        candidates = [c for c in self._docs.values() if c.entry.service_scope == scope]
        if not candidates:
            return None

        matrix = np.array([c.entry.vector for c in candidates], dtype=np.float32)
        similarities = np.dot(matrix, np.asarray(vector, dtype=np.float32))
        best_idx = int(np.argmax(similarities))
        best_similarity = float(similarities[best_idx])
        if best_similarity < threshold:
            return None

        best = candidates[best_idx].entry
        return VectorHit(
            doc_id=best.doc_id,
            query=best.query,
            service_scope=best.service_scope,
            report_json=best.report_json,
            similarity=best_similarity,
        )

    async def put(self, entry: VectorEntry, ttl: float) -> None:
        """Store `entry` for `ttl` seconds, evicting the oldest entry when at capacity."""
        # A vector whose size isn't `dimension` raises ValueError and stores nothing.
        if not self._fits(entry.vector):
            raise ValueError(
                f"vector has {np.size(entry.vector)} dimensions, expected {self.dimension}"
            )
        if entry.doc_id not in self._docs and len(self._docs) >= self.max_capacity:
            self._docs.pop(next(iter(self._docs)), None)
        self._docs[entry.doc_id] = _Stored(entry=entry, expires_at=self._clock() + ttl)

    async def delete(self, doc_id: str) -> None:
        """Remove the entry stored under `doc_id`; an unknown id is a no-op."""
        self._docs.pop(doc_id, None)

    async def clear(self) -> None:
        """Remove every entry."""
        self._docs.clear()

    @property
    def count(self) -> int:
        """Number of entries held, including any expired ones not yet purged by a search."""
        return len(self._docs)

    def _fits(self, vector: np.ndarray) -> bool:
        """True if `vector` is 1-d with `dimension` elements (any size when unset)."""
        shape = np.shape(vector)
        return len(shape) == 1 and (self.dimension is None or shape[0] == self.dimension)

    def _purge_expired(self) -> None:
        """Drop entries whose TTL has passed."""
        now = self._clock()
        for doc_id in [k for k, v in self._docs.items() if v.expires_at < now]:
            self._docs.pop(doc_id, None)
