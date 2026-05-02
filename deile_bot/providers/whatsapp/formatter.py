"""WhatsAppFormatter — MarkupAST -> WhatsApp text (limited markdown)."""

from __future__ import annotations

from typing import List

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
from deile_bot.foundation.output_formatter import OutputFormatter


class WhatsAppFormatter(OutputFormatter):
    name = "whatsapp"
    max_message_chars = 4096

    def render(self, ast: MarkupAST) -> str:
        if not ast:
            return ""
        out: List[str] = []
        for span in ast:
            out.append(self._render_span(span))
        return "".join(out)

    def _render_span(self, span: MarkupSpan) -> str:
        k, t = span.kind, span.text
        if k == SpanKind.PLAIN:
            return t
        if k == SpanKind.BOLD:
            return f"*{t}*"
        if k == SpanKind.ITALIC:
            return f"_{t}_"
        if k == SpanKind.STRIKE:
            return f"~{t}~"
        if k == SpanKind.CODE_INLINE:
            return f"`{t}`"
        if k == SpanKind.CODE_BLOCK:
            return f"```\n{t}\n```"
        if k == SpanKind.QUOTE:
            return "\n".join(f"> {line}" for line in t.split("\n"))
        if k == SpanKind.LINK:
            url = span.meta.get("url", "") if span.meta else ""
            return f"{t} ({url})" if url else t
        if k == SpanKind.HEADING:
            return f"*{t}*\n"
        if k == SpanKind.BULLET:
            return f"• {t}"
        if k == SpanKind.NUMBERED:
            idx = span.meta.get("index", "1") if span.meta else "1"
            return f"{idx}. {t}"
        if k == SpanKind.LINE_BREAK:
            return "\n"
        return t
