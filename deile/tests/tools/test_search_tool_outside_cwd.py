"""Regression test for ``SearchTool`` when scanning paths outside cwd.

Bug: the inner loop formatted a match's ``file_path`` via
``file_path.relative_to(Path.cwd())``. When the search target was
*outside* the cwd (e.g. ``path="/tmp"`` while running from
``/home/user/project``), ``relative_to`` raised ``ValueError``. That
exception is NOT in the except list of ``_search_file_optimized``;
it escaped and was caught by the broad ``except Exception`` in the
futures-result loop, silently dropping ALL matches for that file.
The user got "Found 0 matches" even when matches existed.

Fix: fall back to an absolute path string when ``relative_to`` fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.tools.base import ToolContext
from deile.tools.search_tool import SearchTool


@pytest.fixture()
def out_of_cwd_file(tmp_path_factory):
    """Create a fixture file in a directory that is NOT a child of cwd."""
    other = tmp_path_factory.mktemp("outside")
    f = other / "target.py"
    f.write_text("def needle():\n    return 42\n", encoding="utf-8")
    return f


async def test_search_returns_matches_for_path_outside_cwd(
    tmp_path, monkeypatch, out_of_cwd_file
):
    """Running search from a different cwd must not lose matches."""
    monkeypatch.chdir(tmp_path)
    assert out_of_cwd_file.is_absolute()
    # Sanity: relative_to(cwd) would raise ValueError on this path.
    with pytest.raises(ValueError):
        out_of_cwd_file.relative_to(Path.cwd())

    tool = SearchTool()
    context = ToolContext(
        user_input="find needle",
        parsed_args={
            "query": "needle",
            "path": str(out_of_cwd_file.parent),
            "file_pattern": "*.py",
        },
    )
    result = await tool.execute(context)
    assert result.is_success, result.message
    # ``data`` carries the structured results; ``message`` carries the rendered output.
    payload = result.data or {}
    assert payload.get("total_matches", 0) >= 1
    # The path in the match should be the absolute fallback.
    first_match = payload["matches"][0]
    assert "target.py" in first_match["file_path"]
