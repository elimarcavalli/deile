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


_FENCE_LANG_RE = re.compile(r"```(\w*)")
_FENCE_RE = re.compile(r"```")


def _codeblock_aware_split(text: str, max_chars: int) -> List[str]:
    """Split into <= max_chars chunks; ensure every chunk has balanced ``` fences.

    Strategy:
    1. If the chunk would split inside an unclosed fence and the fence's
       closing ``` is reachable within `max_chars * 2`, extend the chunk to
       absorb the whole fence (preserves byte-for-byte concatenation).
    2. Otherwise, close the fence with `\\n``` and reopen the next chunk with
       `````<lang>\\n`. Joined output then differs from input by the inserted
       fence markers, but every chunk renders correctly (ADV-D14, decision D10).
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    cursor = 0
    pending_lang = ""
    while cursor < len(text):
        prefix = f"```{pending_lang}\n" if pending_lang else ""
        # reserve room for prefix; closing fence is added only when needed
        reserve = len(prefix)
        budget = max(1, max_chars - reserve)
        remaining = len(text) - cursor
        end_offset = min(budget, remaining)
        snippet = text[cursor:cursor + end_offset]
        is_last = (cursor + end_offset) >= len(text)
        # Try to align on newline in back half
        if not is_last:
            last_nl = snippet.rfind("\n")
            if last_nl > end_offset // 2:
                end_offset = last_nl + 1
                snippet = text[cursor:cursor + end_offset]
                is_last = (cursor + end_offset) >= len(text)
        # Count net fence parity in this snippet (flipping pending_lang state)
        matches = _FENCE_LANG_RE.findall(snippet)
        starts_inside = bool(pending_lang)
        ends_inside = starts_inside if len(matches) % 2 == 0 else not starts_inside
        if ends_inside and not is_last:
            # 1) Try to extend to swallow the closing ``` if within 2x budget
            extended_close = text.find("```", cursor + end_offset)
            extended_end = extended_close + 3 if extended_close != -1 else -1
            if (
                extended_end != -1
                and (extended_end - cursor) <= max_chars * 2
            ):
                # absorb the rest of the fence
                end_offset = extended_end - cursor
                snippet = text[cursor:cursor + end_offset]
                # re-evaluate is_last
                is_last = (cursor + end_offset) >= len(text)
                # parity now should be balanced; just append
                chunk = prefix + snippet
                pending_lang = ""
            else:
                # 2) Close + reopen
                last_lang = matches[-1] if matches else pending_lang
                chunk = prefix + snippet + "\n```"
                pending_lang = last_lang
        elif ends_inside and is_last:
            # final chunk left open — close it
            chunk = prefix + snippet + "\n```"
            pending_lang = ""
        else:
            chunk = prefix + snippet
            pending_lang = ""
        chunks.append(chunk)
        cursor += end_offset
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
