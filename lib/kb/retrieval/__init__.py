"""Standalone knowledge retrieval and ContextPack construction."""

from .contracts import ContextPack, RetrievalRequest
from .context import RetrievalService, build_context
from .embedding import EmbeddingError, OpenAICompatibleEmbeddingProvider
from .index import build_retrieval_index
from .reranker import (
    OpenAICompatibleRerankProvider,
    RerankError,
    RerankProvider,
    SentenceTransformerCrossEncoderProvider,
)
from .settings import VectorSettings, create_rerank_provider, create_vector_store

__all__ = [
    "ContextPack",
    "RetrievalRequest",
    "RetrievalService",
    "build_context",
    "build_retrieval_index",
    "EmbeddingError",
    "OpenAICompatibleEmbeddingProvider",
    "RerankError",
    "RerankProvider",
    "OpenAICompatibleRerankProvider",
    "SentenceTransformerCrossEncoderProvider",
    "VectorSettings",
    "create_rerank_provider",
    "create_vector_store",
]
