"""Tests for DiscordOutputFormatter."""

from __future__ import annotations

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
from deile_bot.providers.discord.formatter import DiscordOutputFormatter


class TestRender:
    def test_plain(self):
        f = DiscordOutputFormatter()
        ast = MarkupAST([MarkupSpan(SpanKind.PLAIN, "hi")])
        assert f.render(ast) == "hi"

    def test_bold_italic_strike(self):
        f = DiscordOutputFormatter()
        ast = MarkupAST([
            MarkupSpan(SpanKind.BOLD, "B"),
            MarkupSpan(SpanKind.PLAIN, " "),
            MarkupSpan(SpanKind.ITALIC, "I"),
            MarkupSpan(SpanKind.PLAIN, " "),
            MarkupSpan(SpanKind.STRIKE, "S"),
        ])
        assert f.render(ast) == "**B** *I* ~~S~~"

    def test_code_inline_block(self):
        f = DiscordOutputFormatter()
        ast = MarkupAST([
            MarkupSpan(SpanKind.CODE_INLINE, "x"),
            MarkupSpan(SpanKind.LINE_BREAK, "\n"),
            MarkupSpan(SpanKind.CODE_BLOCK, "print(1)", meta={"language": "python"}),
        ])
        out = f.render(ast)
        assert "`x`" in out
        assert "```python\nprint(1)\n```" in out

    def test_quote_multiline(self):
        f = DiscordOutputFormatter()
        ast = MarkupAST([MarkupSpan(SpanKind.QUOTE, "line1\nline2")])
        out = f.render(ast)
        assert "> line1" in out
        assert "> line2" in out

    def test_heading_level_clamped(self):
        f = DiscordOutputFormatter()
        ast = MarkupAST([MarkupSpan(SpanKind.HEADING, "Big", meta={"level": 5})])
        out = f.render(ast)
        assert out.startswith("### ")  # clamped to 3

    def test_bullet_numbered(self):
        f = DiscordOutputFormatter()
        ast = MarkupAST([
            MarkupSpan(SpanKind.BULLET, "a\n"),
            MarkupSpan(SpanKind.NUMBERED, "b\n", meta={"index": "2"}),
        ])
        out = f.render(ast)
        assert "- a" in out
        assert "2. b" in out

    def test_link(self):
        f = DiscordOutputFormatter()
        ast = MarkupAST([MarkupSpan(SpanKind.LINK, "doc", meta={"url": "http://x"})])
        assert f.render(ast) == "[doc](http://x)"


class TestSplit:
    def test_short_kept(self):
        f = DiscordOutputFormatter()
        assert f.split("a" * 100) == ["a" * 100]

    def test_long_split(self):
        f = DiscordOutputFormatter()
        text = "x" * 5000
        chunks = f.split(text)
        assert len(chunks) >= 3
        assert all(len(c) <= 2000 for c in chunks)

    def test_codeblock_preserved(self):
        """A short codeblock should not be split mid-fence."""
        f = DiscordOutputFormatter()
        text = "abc\n```py\n" + ("y\n" * 100) + "```\nzzz"
        chunks = f.split(text)
        # No chunk should have an orphan ``` (odd count).
        for c in chunks:
            assert c.count("```") % 2 == 0
