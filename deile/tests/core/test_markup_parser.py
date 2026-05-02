"""Tests for MarkdownToASTParser."""

from __future__ import annotations

import pytest

from deile.common.markup_ast import MarkupAST, SpanKind
from deile.ui.markup import MarkdownToASTParser


def kinds(ast: MarkupAST) -> list:
    return [s.kind for s in ast]


class TestParser:
    def test_plain_text(self):
        p = MarkdownToASTParser()
        ast = p.parse("hello world")
        assert kinds(ast) == [SpanKind.PLAIN]
        assert ast[0].text == "hello world"

    def test_bold(self):
        p = MarkdownToASTParser()
        ast = p.parse("oi **mundo** legal")
        ks = kinds(ast)
        assert SpanKind.BOLD in ks
        # find bold span
        bold = [s for s in ast if s.kind == SpanKind.BOLD][0]
        assert bold.text == "mundo"

    def test_italic(self):
        p = MarkdownToASTParser()
        ast = p.parse("oi *mundo*")
        assert SpanKind.ITALIC in kinds(ast)

    def test_code_inline(self):
        p = MarkdownToASTParser()
        ast = p.parse("use `os.path` here")
        codes = [s for s in ast if s.kind == SpanKind.CODE_INLINE]
        assert codes and codes[0].text == "os.path"

    def test_code_block(self):
        p = MarkdownToASTParser()
        ast = p.parse("texto\n```python\ndef foo():\n    pass\n```\nfim")
        cb = [s for s in ast if s.kind == SpanKind.CODE_BLOCK]
        assert cb
        assert cb[0].meta.get("language") == "python"
        assert "def foo()" in cb[0].text

    def test_heading(self):
        p = MarkdownToASTParser()
        ast = p.parse("# Title\nbody")
        head = [s for s in ast if s.kind == SpanKind.HEADING]
        assert head and head[0].meta["level"] == 1
        assert head[0].text == "Title"

    def test_heading_h3(self):
        p = MarkdownToASTParser()
        ast = p.parse("### Sub")
        head = [s for s in ast if s.kind == SpanKind.HEADING]
        assert head[0].meta["level"] == 3

    def test_bullets(self):
        p = MarkdownToASTParser()
        ast = p.parse("- one\n- two\n- three")
        bullets = [s for s in ast if s.kind == SpanKind.BULLET]
        assert len(bullets) == 3
        assert bullets[0].text == "one"

    def test_numbered(self):
        p = MarkdownToASTParser()
        ast = p.parse("1. first\n2. second")
        nums = [s for s in ast if s.kind == SpanKind.NUMBERED]
        assert len(nums) == 2
        assert nums[0].meta["index"] == "1"

    def test_quote(self):
        p = MarkdownToASTParser()
        ast = p.parse("> quoted text")
        q = [s for s in ast if s.kind == SpanKind.QUOTE]
        assert q and q[0].text == "quoted text"

    def test_link(self):
        p = MarkdownToASTParser()
        ast = p.parse("see [docs](http://x.test) ok")
        links = [s for s in ast if s.kind == SpanKind.LINK]
        assert links and links[0].text == "docs"
        assert links[0].meta["url"] == "http://x.test"

    def test_strike(self):
        p = MarkdownToASTParser()
        ast = p.parse("~~old~~ new")
        st = [s for s in ast if s.kind == SpanKind.STRIKE]
        assert st and st[0].text == "old"

    def test_empty(self):
        p = MarkdownToASTParser()
        ast = p.parse("")
        assert len(ast) == 0

    def test_round_trip_text_preserved(self):
        """to_plain of parsed text contains original text (modulo markup chars)."""
        p = MarkdownToASTParser()
        text = "hello world\nthis is a test"
        ast = p.parse(text)
        # to_plain may not be byte-identical (markup stripped), but content is
        joined = ast.to_plain()
        assert "hello world" in joined
        assert "this is a test" in joined
