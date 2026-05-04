"""Concrete adapters for the :mod:`deile.core.interfaces.selector` port."""

from .prompt_toolkit_selector import (PromptToolkitSelector,
                                      get_default_selector)

__all__ = ["PromptToolkitSelector", "get_default_selector"]
