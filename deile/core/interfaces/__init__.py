"""Domain-level protocols and abstractions for adapters in `deile/infrastructure`."""

from .selector import InteractiveSelector, SelectorNotSupported, SelectorOption

__all__ = [
    "InteractiveSelector",
    "SelectorNotSupported",
    "SelectorOption",
]
