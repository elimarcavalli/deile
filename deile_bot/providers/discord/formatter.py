"""DiscordOutputFormatter — MarkupAST -> Discord markdown text."""

from __future__ import annotations

from typing import List

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
from deile_bot.foundation.output_formatter import OutputFormatter


class DiscordOutputFormatter(OutputFormatter):
    name = "discord"
    max_message_chars = 2000

    def render(self, ast: MarkupAST) -> str:
        if not ast:
            return ""
        out: List[str] = []
        for span in ast:
            out.append(self._render_span(span))
        return "".join(out)

    def _render_span(self, span: MarkupSpan) -> str:
        k = span.kind
        t = span.text
        if k == SpanKind.PLAIN:
            return t
        if k == SpanKind.BOLD:
            return f"**{t}**"
        if k == SpanKind.ITALIC:
            return f"*{t}*"
        if k == SpanKind.STRIKE:
            return f"~~{t}~~"
        if k == SpanKind.CODE_INLINE:
            return f"`{t}`"
        if k == SpanKind.CODE_BLOCK:
            lang = span.meta.get("language", "") if span.meta else ""
            return f"```{lang}\n{t}\n```"
        if k == SpanKind.QUOTE:
            return "\n".join(f"> {line}" for line in t.split("\n"))
        if k == SpanKind.LINK:
            url = span.meta.get("url", "") if span.meta else ""
            return f"[{t}]({url})" if url else t
        if k == SpanKind.HEADING:
            level = int(span.meta.get("level", 1) if span.meta else 1)
            level = max(1, min(3, level))
            return f"{'#' * level} {t}\n"
        if k == SpanKind.BULLET:
            return f"- {t}"
        if k == SpanKind.NUMBERED:
            idx = span.meta.get("index", "1") if span.meta else "1"
            return f"{idx}. {t}"
        if k == SpanKind.LINE_BREAK:
            return "\n"
        return t
