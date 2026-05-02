"""MarkupAST — provider-agnostic representation of formatted text.

Lives in `deile.common` so that both DEILE core (CLI/streaming) and
`deile_bot.foundation` (provider rendering) import the same types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping


class SpanKind(str, Enum):
    PLAIN = "plain"
    BOLD = "bold"
    ITALIC = "italic"
    STRIKE = "strike"
    CODE_INLINE = "code_inline"
    CODE_BLOCK = "code_block"
    QUOTE = "quote"
    LINK = "link"
    HEADING = "heading"
    BULLET = "bullet"
    NUMBERED = "numbered"
    LINE_BREAK = "linebreak"


_EMPTY_META: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class MarkupSpan:
    kind: SpanKind
    text: str
    meta: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_META)


class MarkupAST(tuple):
    """Flat sequence of MarkupSpan. No nesting; order matters."""

    __slots__ = ()

    def __new__(cls, spans: Iterable[MarkupSpan] = ()) -> "MarkupAST":
        return super().__new__(cls, tuple(spans))

    @classmethod
    def from_plain(cls, text: str) -> "MarkupAST":
        if not text:
            return cls(())
        return cls((MarkupSpan(kind=SpanKind.PLAIN, text=text),))

    def to_plain(self) -> str:
        return "".join(span.text for span in self)
