"""Stub EmbeddingStore - kept lightweight; embedding pipeline is opt-in."""

from typing import Any, Iterable, List, Optional


class EmbeddingStore:
    """No-op embedding store. Replace with a real implementation when needed."""

    def __init__(self, *_: Any, **__: Any) -> None:
        self._items: list[dict[str, Any]] = []

    def add(self, text: str, metadata: Optional[dict] = None) -> None:
        self._items.append({"text": text, "metadata": metadata or {}})

    def search(self, _query: str, top_k: int = 5) -> List[dict[str, Any]]:
        return self._items[:top_k]

    def clear(self) -> None:
        self._items.clear()

    def __iter__(self) -> Iterable[dict[str, Any]]:
        return iter(self._items)
