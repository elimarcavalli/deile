"""Interactive controls (buttons, lists, quick replies).

Provider-agnostic abstractions; rendered by `OutputFormatter` per provider.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Optional, Tuple


class InteractiveControls(ABC):
    """Marker base for any interactive control set."""


@dataclass(frozen=True, slots=True)
class InteractiveButton:
    label: str
    callback_data: Optional[str] = None
    url: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("label must be non-empty")
        if self.callback_data is None and self.url is None:
            raise ValueError("button needs either callback_data or url")


@dataclass(frozen=True, slots=True)
class InteractiveButtonRow(InteractiveControls):
    buttons: Tuple[InteractiveButton, ...]

    def __post_init__(self) -> None:
        if not self.buttons:
            raise ValueError("button row must have at least 1 button")
        if len(self.buttons) > 5:
            raise ValueError("max 5 buttons per row (Discord limit)")


@dataclass(frozen=True, slots=True)
class InteractiveListSection:
    title: str
    items: Tuple[InteractiveButton, ...]

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("section title must be non-empty")
        if len(self.items) > 10:
            raise ValueError("max 10 items per section (WhatsApp limit)")


@dataclass(frozen=True, slots=True)
class InteractiveList(InteractiveControls):
    button_label: str
    sections: Tuple[InteractiveListSection, ...]

    def __post_init__(self) -> None:
        if not self.sections:
            raise ValueError("list must have at least 1 section")


@dataclass(frozen=True, slots=True)
class QuickReply:
    label: str
    payload: str


@dataclass(frozen=True, slots=True)
class QuickReplies(InteractiveControls):
    options: Tuple[QuickReply, ...]

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError("quick replies must have at least 1 option")
        if len(self.options) > 13:
            raise ValueError("max 13 quick reply options (Messenger limit)")
