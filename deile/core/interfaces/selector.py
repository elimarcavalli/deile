"""Interactive selector — domain contract.

Hexagonal split: this module is the *port*. The terminal-side implementation
(prompt_toolkit, curses, fakes for tests) lives under
``deile/infrastructure/selectors/``. Domain code MUST depend on this module
only, never on the adapter.

Consumers obtain a concrete selector via :func:`get_default_selector`. Tests
inject fakes implementing :class:`InteractiveSelector` directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from ..exceptions import DEILEError


@dataclass(frozen=True)
class SelectorOption:
    """A single navigable item shown to the user.

    ``label`` is the display string. ``value`` is the opaque payload returned
    to the caller — typically an identifier the consumer interprets.
    ``description`` is an optional hint rendered next to the label.
    ``metadata`` is free-form context (e.g. provider/tier for model rows) and
    is matched by incremental search alongside the label when populated.
    """

    label: str
    value: Any
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SelectorCancelled(DEILEError):
    """Raised by adapters that opt to signal cancellation as an exception.

    Adapters MAY also return ``None`` from :meth:`InteractiveSelector.select`
    to indicate cancellation; consumers should accept both. This exception is
    intended for code paths that want a hard stop instead of a sentinel.
    """


class SelectorNotSupported(DEILEError):
    """Raised when the runtime environment cannot host an interactive selector.

    Adapters surface this when they would otherwise block forever (no TTY,
    redirected stdin, headless CI, bot-driven sessions). Consumers should
    catch it and degrade gracefully — e.g. fall back to a printed table plus
    a hint to use the equivalent non-interactive command.
    """


class InteractiveSelector(ABC):
    """Port for keyboard-driven single-select pickers.

    Implementations are expected to:

    - render ``options`` as a navigable list,
    - handle ↑/↓ navigation, Enter to confirm, ESC to cancel,
    - filter incrementally as the user types (substring, case-insensitive),
    - return the chosen :class:`SelectorOption` on Enter, or ``None`` on ESC.

    Single-select only — multi-select is intentionally out of scope for this
    iteration (see issue #63).
    """

    @abstractmethod
    def is_supported(self) -> bool:
        """Return ``True`` iff the selector can run in the current environment.

        Consumers MUST check this before calling :meth:`select`. A ``False``
        return signals that the runtime lacks an interactive TTY (pipe,
        captured stdout, headless CI, bot session) and the consumer should
        choose a non-interactive fallback rather than invoking ``select``.
        """

    @abstractmethod
    async def select(
        self,
        options: Sequence[SelectorOption],
        *,
        prompt: str = "Select an option",
        default_index: int = 0,
    ) -> Optional[SelectorOption]:
        """Render the picker and return the user's choice.

        Args:
            options: Items to show. MUST be non-empty.
            prompt: Header line shown above the list.
            default_index: Initially highlighted row, clamped to range.

        Returns:
            The chosen :class:`SelectorOption`, or ``None`` if the user
            cancelled (ESC). Implementations MAY raise :class:`SelectorCancelled`
            instead of returning ``None`` if that suits their flow.

        Raises:
            ValueError: If ``options`` is empty.
            SelectorNotSupported: If invoked in an unsupported environment.
        """
