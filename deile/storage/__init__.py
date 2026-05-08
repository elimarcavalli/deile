"""Storage module for DEILE - logs and embeddings."""

from .embeddings import EmbeddingStore
from .logs import get_logger

__all__ = ["get_logger", "EmbeddingStore"]
