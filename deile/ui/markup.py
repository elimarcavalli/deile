"""Markdown → MarkupAST parser for DEILE-side rendering and bot output formatting.

Lives in deile/ui/ but is consumed by both DEILE (CLI/streaming) and
deilebot.foundation (provider-agnostic rendering). See master plan §2.1:
shared types live in `deile.common.markup_ast`.

This parser is intentionally minimal — it covers the spans we need for bot
output without reaching for a full CommonMark grammar.
"""

from __future__ import annotations

import re
from typing import List

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[*\-+]\s+(.*)$", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^(\d+)\.\s+(.*)$", re.MULTILINE)
_QUOTE_RE = re.compile(r"^>\s+(.*)$", re.MULTILINE)


class MarkdownToASTParser:
    """Pragmatic markdown → MarkupAST. Flat span sequence; order preserved."""

    def parse(self, text: str) -> MarkupAST:
        if not text:
            return MarkupAST(())
        spans: List[MarkupSpan] = []
        # Pre-extract fenced code blocks first so internal ``` aren't messed with.
        cursor = 0
        for m in _FENCE_RE.finditer(text):
            if m.start() > cursor:
                spans.extend(self._parse_inline(text[cursor : m.start()]))
            language = m.group(1) or ""
            body = m.group(2)
            spans.append(
                MarkupSpan(
                    kind=SpanKind.CODE_BLOCK,
                    text=body,
                    meta={"language": language} if language else {},
                )
            )
            cursor = m.end()
        if cursor < len(text):
            spans.extend(self._parse_inline(text[cursor:]))
        return MarkupAST(spans)

    def _parse_inline(self, text: str) -> List[MarkupSpan]:
        if not text:
            return []
        out: List[MarkupSpan] = []
        # Process line-level structures first (heading, bullet, numbered, quote).
        lines = text.split("\n")
        for i, line in enumerate(lines):
            spans = self._classify_line(line)
            out.extend(spans)
            if i < len(lines) - 1:
                out.append(MarkupSpan(SpanKind.LINE_BREAK, "\n"))
        # Coalesce consecutive PLAIN spans for compactness — keeps order.
        return _coalesce_plain(out)

    def _classify_line(self, line: str) -> List[MarkupSpan]:
        if not line.strip():
            return []
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            return [MarkupSpan(SpanKind.HEADING, m.group(2), meta={"level": level})]
        m = _NUMBERED_RE.match(line)
        if m:
            return [MarkupSpan(SpanKind.NUMBERED, m.group(2), meta={"index": m.group(1)})]
        m = _BULLET_RE.match(line)
        if m:
            return [MarkupSpan(SpanKind.BULLET, m.group(1))]
        m = _QUOTE_RE.match(line)
        if m:
            return [MarkupSpan(SpanKind.QUOTE, m.group(1))]
        return self._parse_inline_styles(line)

    def _parse_inline_styles(self, text: str) -> List[MarkupSpan]:
        # Walk through inline tokens in priority: code > bold > italic > strike > link.
        # Simpler approach: replace patterns left-to-right.
        out: List[MarkupSpan] = []
        cursor = 0
        regex_map = [
            (_INLINE_CODE_RE, SpanKind.CODE_INLINE, lambda m: (m.group(1), {})),
            (_BOLD_RE, SpanKind.BOLD, lambda m: (m.group(1), {})),
            (_STRIKE_RE, SpanKind.STRIKE, lambda m: (m.group(1), {})),
            (_LINK_RE, SpanKind.LINK, lambda m: (m.group(1), {"url": m.group(2)})),
            (_ITALIC_RE, SpanKind.ITALIC, lambda m: (m.group(1), {})),
        ]
        # Find earliest match across all patterns; recurse on remaining tail.
        while cursor < len(text):
            best = None
            for pattern, kind, extract in regex_map:
                m = pattern.search(text, cursor)
                if m and (best is None or m.start() < best[0].start()):
                    best = (m, kind, extract)
            if best is None:
                out.append(MarkupSpan(SpanKind.PLAIN, text[cursor:]))
                break
            m, kind, extract = best
            if m.start() > cursor:
                out.append(MarkupSpan(SpanKind.PLAIN, text[cursor : m.start()]))
            inner_text, meta = extract(m)
            out.append(MarkupSpan(kind, inner_text, meta=meta))
            cursor = m.end()
        return out


def _coalesce_plain(spans: List[MarkupSpan]) -> List[MarkupSpan]:
    if not spans:
        return spans
    out: List[MarkupSpan] = []
    buf = ""
    for s in spans:
        if s.kind == SpanKind.PLAIN:
            buf += s.text
            continue
        if buf:
            out.append(MarkupSpan(SpanKind.PLAIN, buf))
            buf = ""
        out.append(s)
    if buf:
        out.append(MarkupSpan(SpanKind.PLAIN, buf))
    return out
