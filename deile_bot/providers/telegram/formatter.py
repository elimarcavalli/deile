"""TelegramOutputFormatter — MarkupAST -> Telegram MarkdownV2."""

from __future__ import annotations

import re
from typing import List

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
from deile_bot.foundation.output_formatter import OutputFormatter

_MDV2_ESCAPE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def escape_markdown_v2(text: str) -> str:
    return _MDV2_ESCAPE.sub(r"\\\1", text)


class TelegramOutputFormatter(OutputFormatter):
    name = "telegram"
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
            return escape_markdown_v2(t)
        if k == SpanKind.BOLD:
            return f"*{escape_markdown_v2(t)}*"
        if k == SpanKind.ITALIC:
            return f"_{escape_markdown_v2(t)}_"
        if k == SpanKind.STRIKE:
            return f"~{escape_markdown_v2(t)}~"
        if k == SpanKind.CODE_INLINE:
            return f"`{t}`"
        if k == SpanKind.CODE_BLOCK:
            lang = span.meta.get("language", "") if span.meta else ""
            return f"```{lang}\n{t}\n```"
        if k == SpanKind.QUOTE:
            return "\n".join(f">{escape_markdown_v2(line)}" for line in t.split("\n"))
        if k == SpanKind.LINK:
            url = span.meta.get("url", "") if span.meta else ""
            return f"[{escape_markdown_v2(t)}]({url})" if url else escape_markdown_v2(t)
        if k == SpanKind.HEADING:
            return f"*{escape_markdown_v2(t)}*\n"
        if k == SpanKind.BULLET:
            return f"• {escape_markdown_v2(t)}"
        if k == SpanKind.NUMBERED:
            idx = span.meta.get("index", "1") if span.meta else "1"
            return f"{idx}\\. {escape_markdown_v2(t)}"
        if k == SpanKind.LINE_BREAK:
            return "\n"
        return escape_markdown_v2(t)
