"""Domain-level protocols and abstractions for adapters in `deile/infrastructure`."""

from .selector import (InteractiveSelector, SelectorCancelled,
                       SelectorNotSupported, SelectorOption)

__all__ = [
    "InteractiveSelector",
    "SelectorCancelled",
    "SelectorNotSupported",
    "SelectorOption",
]
