"""Tests for the hardened markdown table renderer.

Covers two distinct concerns:

1. Layout — :class:`BetterTableElement` must use vertical separators,
   expand to full console width, and wrap long unbreakable tokens
   (paths, URLs) instead of truncating them with an ellipsis.

2. Streaming jitter — :func:`safe_streaming_split` must defer rendering
   of any trailing in-progress GFM table block, while leaving everything
   else (closed tables, prose, partial inline runs) untouched.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from deile.ui.markdown_table import DeileMarkdown, safe_streaming_split


def _render(markup: str, width: int = 80) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=width, force_terminal=False, color_system=None)
    console.print(DeileMarkdown(markup))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Layout — BetterTableElement
# ---------------------------------------------------------------------------


class TestTableLayout:
    def test_long_unbreakable_tokens_wrap_instead_of_truncating(self):
        markup = (
            "| Path | Note |\n"
            "|---|---|\n"
            "| /a/very/long/path/that/cannot/be/broken/at/spaces.py | x |\n"
        )
        out = _render(markup, width=60)
        # No ellipsis — default ``rich.markdown.TableElement`` truncates
        # long unbreakable tokens with ``…`` and silently drops data;
        # ``overflow="fold"`` must wrap mid-string instead.
        assert "…" not in out
        # All distinctive fragments of the path appear somewhere in the
        # rendered output. ``overflow="fold"`` may break the path across
        # several lines (e.g. ``…spaces.p\ny…``) so we don't require it
        # as one contiguous substring; we just require that the
        # information wasn't dropped.
        for fragment in ("/a/very/", "broken", "spaces.p"):
            assert fragment in out, f"missing fragment {fragment!r} in:\n{out}"

    def test_table_expands_to_full_width(self):
        markup = "| a | b |\n|---|---|\n| 1 | 2 |\n"
        out = _render(markup, width=60)
        # At least one rendered line must reach the full configured width
        # (modulo trailing whitespace stripping). With expand=True every
        # border row spans the console width.
        line_lengths = [len(line) for line in out.splitlines() if line.strip()]
        assert max(line_lengths) >= 55

    def test_columns_have_vertical_separators(self):
        markup = "| a | b |\n|---|---|\n| 1 | 2 |\n"
        out = _render(markup, width=80)
        # ROUNDED box draws │ as the vertical separator between cells.
        assert "│" in out

    def test_table_followed_by_prose_renders_both(self):
        markup = (
            "Antes:\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
            "Depois."
        )
        out = _render(markup, width=80)
        assert "Antes:" in out
        assert "Depois." in out
        assert "│" in out


# ---------------------------------------------------------------------------
# Streaming jitter — safe_streaming_split
# ---------------------------------------------------------------------------


class TestSafeStreamingSplit:
    def test_empty(self):
        assert safe_streaming_split("") == ("", "")

    def test_plain_prose_is_all_stable(self):
        assert safe_streaming_split("just prose with no table") == (
            "just prose with no table",
            "",
        )

    def test_inline_pipe_in_prose_is_not_a_table(self):
        text = "the operator yes | no is just prose"
        assert safe_streaming_split(text) == (text, "")

    def test_open_table_at_buffer_end_is_split_off(self):
        text = "prose\n\n| a |\n|---|\n| 1 |\n"
        prefix, tail = safe_streaming_split(text)
        assert prefix == "prose\n\n"
        assert tail == "| a |\n|---|\n| 1 |\n"

    def test_table_terminated_by_blank_line_is_stable(self):
        text = "prose\n\n| a |\n|---|\n| 1 |\n\n"
        prefix, tail = safe_streaming_split(text)
        assert prefix == text
        assert tail == ""

    def test_table_followed_by_prose_is_stable(self):
        text = "prose\n\n| a |\n|---|\n| 1 |\n\nmore prose"
        prefix, tail = safe_streaming_split(text)
        assert prefix == text
        assert tail == ""

    def test_table_inside_code_fence_is_ignored(self):
        text = "```\n| not a table |\n```"
        prefix, tail = safe_streaming_split(text)
        assert prefix == text
        assert tail == ""

    def test_open_code_fence_holds_everything(self):
        # Open fences are governed by the existing fence guard (not by
        # this split), so the whole buffer is returned as prefix and the
        # caller's other logic decides whether to render it.
        text = "```python\n| code |"
        prefix, tail = safe_streaming_split(text)
        assert prefix == text
        assert tail == ""

    def test_only_header_no_separator_is_held_back(self):
        # Even before the separator arrives, the header line starts with
        # `|` so we should hold it back to avoid rendering it as a
        # paragraph, then re-rendering it as a table once the separator
        # lands (the visible "jump" we're eliminating).
        text = "| Col A | Col B |\n"
        prefix, tail = safe_streaming_split(text)
        assert prefix == ""
        assert tail == text

    def test_partial_separator_is_held_back(self):
        text = "| a | b |\n|---"
        prefix, tail = safe_streaming_split(text)
        assert prefix == ""
        assert tail == text

    def test_progressive_buildup_eventually_stabilises(self):
        # Walk through the same stream of deltas the renderer would see.
        # The first delta is pure prose (no table yet), then deltas 1..4
        # build the open table (tail starts with `|`), then delta 5 closes
        # the table with a blank line so everything snaps into the prefix.
        deltas = [
            "Tabela:\n\n",
            "| a | b |\n",
            "|---|---|\n",
            "| 1 | 2 |\n",
            "| 3 | 4 |\n",
            "\nFim.",
        ]
        acc = deltas[0]
        prefix, tail = safe_streaming_split(acc)
        assert tail == ""
        assert prefix == acc

        for d in deltas[1:5]:
            acc += d
            prefix, tail = safe_streaming_split(acc)
            assert tail.startswith("|"), f"expected open-table tail at {acc!r}"
            assert "Tabela:" in prefix

        acc += deltas[5]
        prefix, tail = safe_streaming_split(acc)
        assert tail == ""
        assert prefix == acc


# ---------------------------------------------------------------------------
# Integration with the streaming renderer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_renderer_prints_rich_renderable_verbatim():
    """RICH_RENDERABLE events (e.g. /model list returning a Table) must be
    handed to ``console.print`` as-is so Rich's width-aware Table layout
    runs at the actual terminal width — not flattened to text and
    re-rendered through Markdown (which shatters the column alignment)."""
    from typing import AsyncIterator, List

    from rich.table import Table

    from deile.core.models.stream_events import (ModelUsageSnapshot,
                                                 StreamEventType,
                                                 UnifiedStreamEvent)
    from deile.ui.streaming_renderer import StreamingRenderer

    async def _replay(events: List[UnifiedStreamEvent]) -> AsyncIterator[UnifiedStreamEvent]:
        for e in events:
            yield e

    table = Table(title="Available Models", show_header=True)
    table.add_column("Provider")
    table.add_column("Model ID")
    table.add_row("anthropic", "claude-opus-4-7")
    table.add_row("openai", "gpt-5")

    events = [
        UnifiedStreamEvent(type=StreamEventType.RICH_RENDERABLE, renderable=table),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=0, output_tokens=0),
        ),
    ]

    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=False, color_system=None)
    renderer = StreamingRenderer(console=console, legacy_windows=False, markdown=True)
    await renderer.render(_replay(events))

    output = buf.getvalue()
    # Rich Table characters survived: title, header, both rows.
    assert "Available Models" in output
    assert "Provider" in output and "Model ID" in output
    assert "anthropic" in output and "claude-opus-4-7" in output
    assert "openai" in output and "gpt-5" in output
    # And — critically — the box-drawing chars Rich uses for tables are
    # present, proving the table was rendered by Rich (not split into
    # scattered ASCII pipes by Markdown's paragraph re-flow).
    assert any(ch in output for ch in ("│", "┃", "─", "━"))


@pytest.mark.asyncio
async def test_streaming_renderer_prints_rich_renderable_verbatim_legacy():
    """Same guarantee on the legacy (non-Live) path."""
    from typing import AsyncIterator, List

    from rich.table import Table

    from deile.core.models.stream_events import (ModelUsageSnapshot,
                                                 StreamEventType,
                                                 UnifiedStreamEvent)
    from deile.ui.streaming_renderer import StreamingRenderer

    async def _replay(events: List[UnifiedStreamEvent]) -> AsyncIterator[UnifiedStreamEvent]:
        for e in events:
            yield e

    table = Table(title="X")
    table.add_column("a")
    table.add_row("1")

    events = [
        UnifiedStreamEvent(type=StreamEventType.RICH_RENDERABLE, renderable=table),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(),
        ),
    ]

    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=False, color_system=None)
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=True)
    await renderer.render(_replay(events))

    output = buf.getvalue()
    assert "X" in output  # title
    assert "a" in output  # header
    assert "1" in output  # data row
    assert any(ch in output for ch in ("│", "┃", "─", "━"))


@pytest.mark.asyncio
async def test_streaming_renderer_defers_open_table_then_commits_on_final_flush():
    """End-to-end: feed deltas that build a table without trailing blank
    line, then close the stream. The final scrollback must contain a
    rendered table (not the dim raw pipes the user sees mid-stream)."""
    from typing import AsyncIterator, List

    from deile.core.models.stream_events import (StreamEventType,
                                                 UnifiedStreamEvent)
    from deile.ui.streaming_renderer import StreamingRenderer

    async def _replay(events: List[UnifiedStreamEvent]) -> AsyncIterator[UnifiedStreamEvent]:
        for e in events:
            yield e

    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Tabela:\n\n"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="| a | b |\n"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="|---|---|\n"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="| 1 | 2 |\n"),
        # No trailing blank line — the agent ended its turn with the table.
    ]

    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=False, color_system=None)
    renderer = StreamingRenderer(console=console, legacy_windows=False, markdown=True)
    result = await renderer.render(_replay(events))

    output = buf.getvalue()
    # Final scrollback must contain rendered table characters, not just
    # the dim raw pipes that streamed during the open-table window.
    assert "│" in output, f"expected table separators in final output:\n{output}"
    assert "Tabela:" in output
    # Cells appear in the rendered table.
    assert "1" in output and "2" in output
    # Aggregate text matches what we sent.
    assert result.full_text == "Tabela:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
