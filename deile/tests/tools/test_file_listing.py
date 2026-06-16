"""Direct unit tests for helpers in ``deile.tools._file_listing``.

The directory-listing helpers were extracted from ``ListFilesTool`` to keep
that tool's execute_sync small. Most coverage existed only through the tool;
this module exercises ``_collect_entries`` and ``_render_tree`` directly so a
regression in either does not require running a full ``ListFilesTool``
scenario to surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.tools._file_listing import (
    _MAX_DIRS_SHOWN,
    _MAX_FILES_SHOWN,
    _collect_entries,
    _render_tree,
)

# ---------------------------------------------------------------------------
# _collect_entries — single file / directory walk / gitignore filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_collect_entries_single_file_branch(tmp_path: Path):
    """When ``full_path`` is a file, only that one entry comes back —
    no gitignore filtering, no walk."""
    f = tmp_path / "solo.txt"
    f.write_text("hi", encoding="utf-8")

    entries = _collect_entries(
        f, tmp_path, recursive=False, show_hidden=False, pattern=None
    )

    assert len(entries) == 1
    assert entries[0]["name"] == "solo.txt"
    assert entries[0]["type"] == "file"
    assert entries[0]["size"] == 2


@pytest.mark.unit
def test_collect_entries_directory_walk_lists_files_and_dirs(tmp_path: Path):
    """Iterdir-mode walk: returns both file and directory entries, sorted."""
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("b", encoding="utf-8")
    (tmp_path / "subdir").mkdir()

    entries = _collect_entries(
        tmp_path, tmp_path, recursive=False, show_hidden=False, pattern=None
    )

    names = [e["name"] for e in entries]
    assert names == sorted(names, key=str.lower)
    types = {e["name"]: e["type"] for e in entries}
    assert types == {
        "alpha.txt": "file",
        "beta.txt": "file",
        "subdir": "directory",
    }


@pytest.mark.unit
def test_collect_entries_respects_gitignore_filter(tmp_path: Path):
    """Files matching a pattern from the project's ``.gitignore`` are excluded."""
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "kept.txt").write_text("k", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("i", encoding="utf-8")

    entries = _collect_entries(
        tmp_path, tmp_path, recursive=False, show_hidden=False, pattern=None
    )

    names = [e["name"] for e in entries]
    assert "kept.txt" in names
    assert "ignored.txt" not in names
    # The .gitignore file itself is hidden by ``show_hidden=False``.
    assert ".gitignore" not in names


@pytest.mark.unit
def test_collect_entries_recursive_string_coercion(tmp_path: Path):
    """LLMs sometimes send ``recursive="True"`` (string) instead of bool —
    the helper must still take the recursive branch."""
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "deep.txt").write_text("d", encoding="utf-8")

    entries = _collect_entries(
        tmp_path, tmp_path, recursive="True", show_hidden=False, pattern=None
    )

    names = [e["name"] for e in entries]
    assert "deep.txt" in names  # rglob picked up the nested file


# ---------------------------------------------------------------------------
# _render_tree — truncation, empty-folder, remainder count
# ---------------------------------------------------------------------------


def _make_entries(num_dirs: int, num_files: int) -> list[dict]:
    """Build a ``files_info`` list with the given dir/file counts."""
    dirs = [
        {"name": f"dir{i:02d}", "type": "directory", "path": f"dir{i:02d}"}
        for i in range(num_dirs)
    ]
    files = [
        {"name": f"file{i:02d}.txt", "type": "file", "path": f"file{i:02d}.txt"}
        for i in range(num_files)
    ]
    return dirs + files


@pytest.mark.unit
def test_render_tree_empty_folder():
    """Empty input renders the ``(pasta vazia)`` marker."""
    output = _render_tree(".", [])
    assert "(pasta vazia)" in output


@pytest.mark.unit
def test_render_tree_under_caps_shows_no_remainder():
    """When counts are below the caps, the "... e mais N itens" line is absent."""
    entries = _make_entries(num_dirs=3, num_files=4)
    output = _render_tree(".", entries)
    assert "e mais" not in output
    assert "dir00" in output
    assert "file00.txt" in output


@pytest.mark.unit
def test_render_tree_truncates_at_caps_and_reports_remainder():
    """Above the caps: only the first ``_MAX_DIRS_SHOWN`` dirs and
    ``_MAX_FILES_SHOWN`` files are listed; the rest are summarized."""
    extra_dirs = 3
    extra_files = 4
    entries = _make_entries(
        num_dirs=_MAX_DIRS_SHOWN + extra_dirs,
        num_files=_MAX_FILES_SHOWN + extra_files,
    )
    output = _render_tree(".", entries)

    # First entry per kind is shown; the entry that pushes past the cap is not.
    assert "dir00" in output
    assert f"dir{_MAX_DIRS_SHOWN:02d}" not in output
    assert "file00.txt" in output
    assert f"file{_MAX_FILES_SHOWN:02d}.txt" not in output

    expected_remaining = extra_dirs + extra_files
    assert f"e mais {expected_remaining} itens" in output


@pytest.mark.unit
def test_render_tree_remainder_counts_hidden_dirs_and_files_together():
    """Regression guard: the old implementation computed ``total_remaining``
    against ``len(files_info)`` instead of ``len(dirs)`` + ``len(files)``,
    which over-counted by ``len(shown_dirs) + len(shown_files)``. Make sure
    the count matches the actual number of hidden entries."""
    entries = _make_entries(
        num_dirs=_MAX_DIRS_SHOWN + 2,
        num_files=_MAX_FILES_SHOWN + 5,
    )
    output = _render_tree(".", entries)
    assert "e mais 7 itens" in output
