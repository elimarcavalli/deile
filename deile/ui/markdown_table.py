"""Markdown table rendering hardened for terminal CLI output.

Two distinct problems with ``rich.markdown.Markdown``'s default table renderer
in a streaming CLI context:

1. **Layout overflow**: ``TableElement`` builds a ``Table(box=SIMPLE_HEAVY)``
   with no ``expand``, no per-column ``overflow`` policy, and no vertical
   separators. When a table contains long unbreakable tokens (file paths,
   URLs, ids), Rich silently truncates with ``…``, losing data. When it
   contains long prose, the lack of vertical rules makes columns hard to
   scan. When the table is narrower than the terminal, the surrounding
   text and the table don't share a width baseline so the response looks
   misaligned.

2. **Streaming jitter**: ``rich.markdown.Markdown`` is invoked on the
   *full* accumulator on every text delta. While a table is being
   streamed, the buffer transitions through states like
   ``"| a | b |\\n"`` (parses as a paragraph),
   ``"| a | b |\\n|---|---|"`` (parses as a 0-row table),
   ``"| a | b |\\n|---|---|\\n| 1"`` (parses as a 1-row table with one
   empty cell) — each of which redraws the Live region with a different
   shape, causing visible glitches.

This module fixes both:

* :class:`BetterTableElement` uses ``box=ROUNDED`` (vertical separators),
  ``expand=True`` (always fills available width), and per-column
  ``overflow="fold"`` (long tokens wrap mid-string instead of being
  truncated).
* :class:`DeileMarkdown` is a thin ``Markdown`` subclass that wires the
  better element into the elements registry without monkey-patching the
  global Rich library.
* :func:`safe_streaming_split` lets the streaming renderer carve a
  trailing in-progress table out of the buffer so it can be deferred
  (rendered as raw dim text) until the table is complete, eliminating
  the jitter.
"""

from __future__ import annotations

import re
from typing import Tuple

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown, TableElement
from rich.table import Table
from rich.text import Text


class BetterTableElement(TableElement):
    """Drop-in replacement for ``rich.markdown.TableElement``.

    Differences vs. the upstream default:

    * ``box=ROUNDED`` draws vertical separators between columns so each
      cell is visually delimited.
    * ``expand=True`` makes the table use the full console width, matching
      the surrounding paragraphs and avoiding the "narrow island" look.
    * Each column and each cell uses ``overflow="fold"`` so long
      unbreakable tokens (paths, URLs) wrap mid-string instead of being
      cut with an ellipsis. Information is preserved at the cost of more
      vertical space — the right trade-off for a CLI transcript.
    """

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        table = Table(
            box=box.ROUNDED,
            show_lines=False,
            expand=True,
            border_style="dim",
            header_style="bold",
            pad_edge=False,
            collapse_padding=False,
        )

        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                table.add_column(
                    column.content,
                    overflow="fold",
                    no_wrap=False,
                )

        if self.body is not None:
            for row in self.body.rows:
                row_content = [
                    Text(
                        str(cell.content),
                        justify=cell.justify,
                        overflow="fold",
                    )
                    for cell in row.cells
                ]
                table.add_row(*row_content)

        yield table


class DeileMarkdown(Markdown):
    """``rich.markdown.Markdown`` with the hardened table element wired in."""

    elements = {**Markdown.elements, "table_open": BetterTableElement}


_TABLE_LINE_RE = re.compile(r"^\s*\|")


def safe_streaming_split(text: str) -> Tuple[str, str]:
    """Split ``text`` into a ``(stable_prefix, transient_tail)`` pair.

    The ``transient_tail`` is non-empty exactly when the buffer ends with
    an *in-progress* GFM table block — i.e. a contiguous run of pipe-led
    lines that hasn't been terminated by a blank line. The streaming
    renderer renders ``stable_prefix`` as Markdown (so closed tables and
    surrounding prose look correct) and ``transient_tail`` as raw dim
    text (so the user still sees the agent typing rows, but without
    Rich's Live region jumping every keystroke as the parse outcome
    flips between paragraph / 0-row table / N-row table).

    On end-of-stream the caller bypasses this split (renders the full
    buffer as Markdown), at which point the trailing table is committed
    cleanly.

    Heuristics, in order:

    * Empty input → ``("", "")``.
    * If the cursor sits inside an open fenced code block (odd count of
      triple backticks), the entire buffer is stable — anything inside a
      code fence is rendered verbatim by Markdown anyway, no jitter to
      avoid.
    * Otherwise, walk backward from the end skipping trailing whitespace.
      If the last non-blank line starts with ``|`` (a candidate table
      row), expand the run upward to all contiguous pipe-led lines.
    * If there's a blank line *after* the run (i.e. trailing whitespace
      contained at least one ``\\n\\n``), the table is closed — return
      ``(text, "")``.
    * Otherwise the table is open — return ``(prefix_before_run, run + trailing_ws)``.
    """
    if not text:
        return "", ""

    if "|" not in text:
        return text, ""

    if text.count("```") % 2 == 1:
        return text, ""

    n = len(text)
    end = n
    while end > 0 and text[end - 1] in (" ", "\t", "\n", "\r"):
        end -= 1
    if end == 0:
        return text, ""

    _tail = text[end:]
    has_trailing_blank_line = "\n\n" in _tail or _tail.count("\n") >= 2

    last_nl = text.rfind("\n", 0, end)
    last_line_start = last_nl + 1 if last_nl != -1 else 0
    if not _TABLE_LINE_RE.match(text[last_line_start:end]):
        return text, ""

    run_start = last_line_start
    while run_start > 0:
        prev_nl = text.rfind("\n", 0, run_start - 1)
        prev_line_start = prev_nl + 1 if prev_nl != -1 else 0
        prev_line = text[prev_line_start:run_start - 1] if run_start > 0 else ""
        if not _TABLE_LINE_RE.match(prev_line):
            break
        run_start = prev_line_start

    if has_trailing_blank_line:
        return text, ""

    return text[:run_start], text[run_start:]
