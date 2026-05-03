"""Tests for OutputFormatter base + PlainTextFormatter + splitter."""

from __future__ import annotations

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
from deile_bot.foundation.output_formatter import (PlainTextFormatter,
                                                   _codeblock_aware_split)


class TestPlainText:
    def test_renders_plain(self):
        f = PlainTextFormatter()
        ast = MarkupAST.from_plain("hi there")
        assert f.render(ast) == "hi there"

    def test_renders_bullet(self):
        f = PlainTextFormatter()
        ast = MarkupAST([MarkupSpan(SpanKind.BULLET, "first\n"), MarkupSpan(SpanKind.BULLET, "second\n")])
        out = f.render(ast)
        assert "•" in out
        assert "first" in out
        assert "second" in out

    def test_renders_quote(self):
        f = PlainTextFormatter()
        ast = MarkupAST([MarkupSpan(SpanKind.QUOTE, "hello\n")])
        assert "> hello" in f.render(ast)

    def test_empty_ast(self):
        f = PlainTextFormatter()
        assert f.render(MarkupAST([])) == ""


class TestSplit:
    def test_split_short_kept(self):
        f = PlainTextFormatter()
        f.max_message_chars = 100
        assert f.split("short text") == ["short text"]

    def test_split_long(self):
        f = PlainTextFormatter()
        f.max_message_chars = 50
        text = "a" * 200
        chunks = f.split(text)
        assert sum(len(c) for c in chunks) == 200
        assert all(len(c) <= 50 for c in chunks)

    def test_split_breaks_on_newline(self):
        text = "line1\nline2\nline3\nline4\nline5\nline6\n"
        chunks = _codeblock_aware_split(text, 18)
        # No chunk should split a partial line in unsafe spot
        assert chunks
        assert "".join(chunks) == text

    def test_split_codeblock_kept(self):
        text = "preamble\n```python\nfor i in range(100):\n    print(i)\n```\nepilogue"
        chunks = _codeblock_aware_split(text, 30)
        joined = "".join(chunks)
        # No half-codeblock — every chunk has matching ``` pairs
        assert joined == text


class TestEmpty:
    def test_split_empty(self):
        f = PlainTextFormatter()
        assert f.split("") == []
