"""OutputFormatter ABC + PlainTextFormatter + codeblock-aware splitter."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import List

from deile.common.markup_ast import MarkupAST, SpanKind


class OutputFormatter(ABC):
    """Render a MarkupAST to a provider-specific text representation."""

    name: str = "abstract"
    max_message_chars: int = 2000

    @abstractmethod
    def render(self, ast: MarkupAST) -> str: ...

    def split(self, text: str) -> List[str]:
        """Split text into chunks <= max_message_chars, codeblock-aware."""
        if not text:
            return []
        if len(text) <= self.max_message_chars:
            return [text]
        return _codeblock_aware_split(text, self.max_message_chars)


def _codeblock_aware_split(text: str, max_chars: int) -> List[str]:
    """Never break inside a ```...``` codeblock; prefer linebreaks."""
    chunks: List[str] = []
    cursor = 0
    code_fence_re = re.compile(r"```")
    while cursor < len(text):
        end = min(cursor + max_chars, len(text))
        if end == len(text):
            chunks.append(text[cursor:end])
            break
        # walk forward and find safe split point
        head = text[cursor:end]
        # detect if head opens an unclosed fence — extend until close OR fall back
        opens = len(code_fence_re.findall(head))
        if opens % 2 == 1:
            # find next ``` after end
            next_close = text.find("```", end)
            if next_close != -1 and (next_close + 3 - cursor) <= max_chars * 2:
                end = next_close + 3
            # else: leave as-is (better to chunk than to balloon)
        # try to break at last newline within head
        cut_at = end
        snippet = text[cursor:end]
        last_newline = snippet.rfind("\n")
        if last_newline > max_chars // 2:
            cut_at = cursor + last_newline + 1
        chunks.append(text[cursor:cut_at])
        cursor = cut_at
    return chunks


class PlainTextFormatter(OutputFormatter):
    """Strips markup; useful for Messenger/Instagram and as a default fallback."""

    name = "plain"
    max_message_chars = 2000

    def render(self, ast: MarkupAST) -> str:
        if not ast:
            return ""
        out: List[str] = []
        for span in ast:
            if span.kind == SpanKind.LINE_BREAK:
                out.append("\n")
            elif span.kind == SpanKind.CODE_BLOCK:
                out.append(f"\n{span.text}\n")
            elif span.kind == SpanKind.HEADING:
                out.append(f"{span.text}\n")
            elif span.kind == SpanKind.BULLET:
                out.append(f"• {span.text}")
            elif span.kind == SpanKind.NUMBERED:
                idx = span.meta.get("index", "1")
                out.append(f"{idx}. {span.text}")
            elif span.kind == SpanKind.QUOTE:
                out.append(f"> {span.text}")
            else:
                out.append(span.text)
        return "".join(out)
