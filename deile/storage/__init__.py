"""Storage module for DEILE - logs and embeddings."""

from .logs import get_logger
from .embeddings import EmbeddingStore

__all__ = ["get_logger", "EmbeddingStore"]
