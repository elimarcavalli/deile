"""Regression tests for ListFilesTool recursion + pattern filtering.

Original bug: the executor computed an ``entries`` iterator (rglob for
recursive, glob for pattern) but the for-loop ignored it and iterated
``full_path.iterdir()`` instead. As a result, ``recursive=True`` did
nothing and ``pattern='*.py'`` did nothing — silent functional failure.

The LLM-side parsed args also arrive as STRINGS ("True"/"False") in some
provider-tool encodings, so the recursive flag must be coerced from str.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.tools.base import ToolContext
from deile.tools.file_tools import ListFilesTool


def _make_ctx(working_dir: Path, **parsed_args) -> ToolContext:
    return ToolContext(
        user_input="list_files",
        working_directory=str(working_dir),
        parsed_args=parsed_args,
    )


@pytest.fixture
def project_tree(tmp_path: Path) -> Path:
    """Build a small tree:

        root/
          a.py
          sub/
            b.py
            inner/
              c.py
          notes.md
    """
    (tmp_path / "a.py").write_text("# a")
    (tmp_path / "notes.md").write_text("md")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("# b")
    inner = sub / "inner"
    inner.mkdir()
    (inner / "c.py").write_text("# c")
    return tmp_path


def test_recursive_true_includes_nested_files(project_tree: Path):
    res = ListFilesTool().execute_sync(
        _make_ctx(project_tree, path=".", recursive=True)
    )
    assert res.is_success, res.message
    paths = {e["path"] for e in (res.data or [])}
    # Nested files MUST appear when recursive=True.
    assert any(p.endswith("b.py") for p in paths), f"sub/b.py missing: {paths}"
    assert any(p.endswith("c.py") for p in paths), f"sub/inner/c.py missing: {paths}"


def test_recursive_string_True_is_coerced(project_tree: Path):
    """The LLM may serialize bool args as the strings 'True'/'False'.
    The tool must treat 'True' as True (coercion), otherwise recursion
    silently degrades to iterdir."""
    res = ListFilesTool().execute_sync(
        _make_ctx(project_tree, path=".", recursive="True")
    )
    assert res.is_success
    paths = {e["path"] for e in (res.data or [])}
    assert any(p.endswith("c.py") for p in paths), (
        "string 'True' was not coerced to bool — nested c.py is missing"
    )


def test_pattern_filters_results(project_tree: Path):
    res = ListFilesTool().execute_sync(
        _make_ctx(project_tree, path=".", recursive=True, pattern="*.py")
    )
    assert res.is_success
    names = {e["name"] for e in (res.data or [])}
    # All .py files appear, the .md file does NOT.
    assert "a.py" in names
    assert "b.py" in names
    assert "c.py" in names
    assert "notes.md" not in names, (
        f"pattern='*.py' did not filter notes.md: {names}"
    )


def test_non_recursive_returns_only_direct_children(project_tree: Path):
    res = ListFilesTool().execute_sync(
        _make_ctx(project_tree, path=".", recursive=False)
    )
    assert res.is_success
    names = {e["name"] for e in (res.data or [])}
    # Direct children only — nested b.py / c.py absent.
    assert "a.py" in names
    assert "sub" in names
    assert "b.py" not in names
    assert "c.py" not in names
