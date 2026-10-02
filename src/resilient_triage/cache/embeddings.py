"""Vector embedding providers for semantic caching."""

import hashlib
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from resilient_triage.config import settings

logger = logging.getLogger(__name__)


def _normalize_vector(vec: np.ndarray) -> np.ndarray:
    """Normalize numpy array to unit L2 length."""
    norm = np.linalg.norm(vec)
    if norm == 0 or np.isnan(norm):
        return vec
    return (vec / norm).astype(np.float32)


class BaseEmbeddingProvider(ABC):
    """Abstract interface for dense vector embedding generation."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector dimensionality."""
        pass

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string into a normalized dense vector."""
        pass

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document strings into normalized dense vectors."""
        pass

    def warmup(self) -> None:
        """Optional pre-warming of model weights."""
        pass


class FastEmbedProvider(BaseEmbeddingProvider):
    """High-performance ONNX-based embedding provider powered by FastEmbed."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.embedding_model
        self._dim = settings.vector_dimension
        self._model: Any = None
        self._lock = threading.Lock()

    def _ensure_model(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    logger.info("Initializing FastEmbed model: %s", self.model_name)
                    from fastembed import TextEmbedding

                    self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def warmup(self) -> None:
        """Pre-warm the embedding model so the first request incurs no latency."""
        self._ensure_model()
        self.embed_query("warmup ping")
        logger.info("FastEmbed model %s pre-warmed successfully.", self.model_name)

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_query(self, text: str) -> list[float]:
        model = self._ensure_model()
        # FastEmbed returns a generator of numpy arrays
        raw_vec = next(model.embed([text]))
        normalized = _normalize_vector(raw_vec)
        return normalized.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        results = []
        for raw_vec in model.embed(texts):
            results.append(_normalize_vector(raw_vec).tolist())
        return results


class DeterministicEmbeddingProvider(BaseEmbeddingProvider):
    """Zero-dependency, offline deterministic vectorizer using hashed token projections.

    Guarantees deterministic L2-normalized vectors without downloading external models.
    Ideal for unit tests, offline environments, and airgapped deployments.
    """

    def __init__(self, dimension: int | None = None) -> None:
        self._dim = dimension or settings.vector_dimension

    @property
    def dimension(self) -> int:
        return self._dim

    def _vectorize(self, text: str) -> np.ndarray:
        normalized_text = text.lower().strip()
        vec = np.zeros(self._dim, dtype=np.float32)

        if not normalized_text:
            return vec

        # Tokenize and include character n-grams for semantic fuzzy overlap
        tokens = normalized_text.split()
        features: list[str] = list(tokens)

        # Add 3-character and 4-character subwords
        for token in tokens:
            if len(token) > 3:
                for i in range(len(token) - 2):
                    features.append(token[i : i + 3])

        # Project features into dense vector space using stable SHA256 hashes
        for feat in features:
            h = int(hashlib.sha256(feat.encode("utf-8")).hexdigest()[:8], 16)
            idx = h % self._dim
            sign = 1.0 if (h >> 16) & 1 else -1.0
            vec[idx] += sign

        return _normalize_vector(vec)

    def embed_query(self, text: str) -> list[float]:
        return self._vectorize(text).tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorize(t).tolist() for t in texts]


_singleton_lock = threading.Lock()
_global_embedding_provider: BaseEmbeddingProvider | None = None


def get_embedding_provider(force_deterministic: bool | None = None) -> BaseEmbeddingProvider:
    """Retrieve process-level embedding provider singleton."""
    global _global_embedding_provider

    use_deterministic = (
        force_deterministic
        if force_deterministic is not None
        else settings.use_deterministic_embeddings
    )

    if _global_embedding_provider is None:
        with _singleton_lock:
            if _global_embedding_provider is None:
                if use_deterministic:
                    _global_embedding_provider = DeterministicEmbeddingProvider()
                else:
                    _global_embedding_provider = FastEmbedProvider()

    return _global_embedding_provider


def reset_embedding_provider() -> None:
    """Reset singleton provider (primarily for test fixture configuration)."""
    global _global_embedding_provider
    with _singleton_lock:
        _global_embedding_provider = None
