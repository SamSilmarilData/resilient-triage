"""Semantic caching components with Redis and in-memory vector drivers."""

from resilient_triage.cache.embeddings import (
    BaseEmbeddingProvider,
    DeterministicEmbeddingProvider,
    FastEmbedProvider,
    get_embedding_provider,
)
from resilient_triage.cache.semantic_cache import (
    CacheMatch,
    SemanticCacheManager,
    format_scoped_query,
    semantic_cache,
)

__all__ = [
    "BaseEmbeddingProvider",
    "FastEmbedProvider",
    "DeterministicEmbeddingProvider",
    "get_embedding_provider",
    "CacheMatch",
    "SemanticCacheManager",
    "format_scoped_query",
    "semantic_cache",
]
