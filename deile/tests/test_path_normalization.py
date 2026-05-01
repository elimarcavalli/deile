"""Exhaustive tests for path normalization in file_tools.

The new ``_resolve_project_path`` function exists to defend against the LLM
mangling paths in every way LLMs typically do — leading slashes mistaken for
system absolute, Windows backslashes, ``~`` home-shorthand, ``@`` prefixes,
escapes via ``..``, drive letters, null-byte injection, etc. This file
catalogues every form we've seen in real transcripts and asserts the resolver
either:

1. Returns a clean absolute path inside the working directory (with a
   ``note`` explaining what was normalized, when applicable), or
2. Raises ``LocalFileAccessViolation`` with a message specific enough that
   the LLM (and a human reading the error) knows exactly what to fix.

Every test uses a fresh ``tmp_path`` so absolute resolutions are stable.

Also covers the integration with ``WriteFileTool`` and ``ReadFileTool``:
the tool result must echo the resolved path so the model can never
hallucinate where a file ended up.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from deile.tools.file_tools import (
    LocalFileAccessViolation,
    ReadFileTool,
    ResolvedPath,
    WriteFileTool,
    _resolve_project_path,
    _validate_path_within_working_directory,
)
from deile.tools.base import ToolContext


# ---------------------------------------------------------------------------
# 1. Pure-function tests for _resolve_project_path
# ---------------------------------------------------------------------------


@pytest.fixture
def cwd(tmp_path) -> str:
    """Project working directory for resolver tests."""
    return str(tmp_path.resolve())


def _resolve(path: str, cwd: str) -> ResolvedPath:
    return _resolve_project_path(path, cwd)


# --- Clean relative paths pass through untouched -------------------------


@pytest.mark.parametrize(
    "input_path, expected_rel",
    [
        ("foo.py", "foo.py"),
        ("tmp/foo.py", "tmp/foo.py"),
        ("a/b/c/d.py", "a/b/c/d.py"),
        ("./foo.py", "foo.py"),
        ("./a/../b.py", "b.py"),  # internal .. resolves inside CWD
        ("a/.//b/.//c.py", "a/b/c.py"),
        (".", "."),  # CWD itself
    ],
)
def test_clean_relative_paths_unchanged(cwd, input_path, expected_rel):
    r = _resolve(input_path, cwd)
    assert r.relative_to_cwd == expected_rel
    assert r.note is None
    assert r.absolute == str(Path(cwd) / expected_rel) or r.absolute == cwd


# --- Leading-slash absolute paths are normalized to project-relative -----


@pytest.mark.parametrize(
    "input_path, expected_rel",
    [
        ("/tmp/foo.py", "tmp/foo.py"),
        ("/tmp/calc/__init__.py", "tmp/calc/__init__.py"),
        ("/etc/passwd", "etc/passwd"),
        ("/usr/local/bin/x.sh", "usr/local/bin/x.sh"),
        ("/data/cache/x.json", "data/cache/x.json"),
        ("/home/user/project/x.py", "home/user/project/x.py"),
    ],
)
def test_leading_slash_normalized_to_project_relative(cwd, input_path, expected_rel):
    r = _resolve(input_path, cwd)
    assert r.relative_to_cwd == expected_rel
    assert r.note is not None
    assert "leading '/' stripped" in r.note
    # The resolved absolute is INSIDE cwd
    assert Path(r.absolute).is_relative_to(Path(cwd).resolve())


def test_multiple_leading_slashes_collapsed(cwd):
    r = _resolve("//tmp/foo.py", cwd)
    assert r.relative_to_cwd == "tmp/foo.py"
    assert r.note is not None


def test_only_slashes_rejected(cwd):
    for inp in ["/", "//", "///", "////"]:
        with pytest.raises(LocalFileAccessViolation, match="just slashes"):
            _resolve(inp, cwd)


# --- Absolute path that's ALREADY inside CWD passes through cleanly ------


def test_absolute_path_inside_cwd_passes_through(cwd):
    """When tools internally resolve a fuzzy match to an absolute path that's
    already within the working directory, we must NOT normalize it — that
    would break legitimate flows like FileResolver.get_best_match."""
    abs_inside = f"{cwd}/foo/bar.py"
    r = _resolve(abs_inside, cwd)
    assert r.note is None
    assert r.relative_to_cwd == "foo/bar.py"
    assert r.absolute == str(Path(cwd) / "foo" / "bar.py")


def test_absolute_path_with_extra_slash_inside_cwd(cwd):
    r = _resolve(f"{cwd}/x/./y.py", cwd)
    assert r.relative_to_cwd == "x/y.py"


# --- @-prefix (DEILE file-reference syntax) ------------------------------


@pytest.mark.parametrize(
    "input_path, expected_rel",
    [
        ("@foo.py", "foo.py"),
        ("@tmp/x.py", "tmp/x.py"),
        ("@/tmp/x.py", "tmp/x.py"),  # @ + leading slash both stripped
        ("@./tmp/x.py", "tmp/x.py"),
    ],
)
def test_at_prefix_stripped(cwd, input_path, expected_rel):
    r = _resolve(input_path, cwd)
    assert r.relative_to_cwd == expected_rel
    assert r.note is not None
    assert "@" in r.note


# --- Home shorthand (~/) is NOT expanded to system $HOME -----------------


@pytest.mark.parametrize(
    "input_path, expected_rel",
    [
        ("~/foo.py", "foo.py"),
        ("~/tmp/x.py", "tmp/x.py"),
        ("~", "."),
    ],
)
def test_home_shorthand_treated_as_project_relative(cwd, input_path, expected_rel):
    r = _resolve(input_path, cwd)
    assert r.relative_to_cwd == expected_rel
    assert r.note is not None
    assert "~" in r.note
    assert Path(r.absolute).is_relative_to(Path(cwd).resolve())


# --- Windows-style paths -------------------------------------------------


@pytest.mark.parametrize(
    "input_path, expected_rel",
    [
        (r"src\main.py", "src/main.py"),
        (r"a\b\c\d.py", "a/b/c/d.py"),
        ("C:\\Users\\x.py", "Users/x.py"),
        ("D:/data/foo.py", "data/foo.py"),
        ("c:\\\\foo.py", "foo.py"),  # quadruple backslashes
    ],
)
def test_windows_paths_normalized(cwd, input_path, expected_rel):
    r = _resolve(input_path, cwd)
    assert r.relative_to_cwd == expected_rel
    assert r.note is not None


# --- Parent-traversal escapes ARE rejected ---------------------------------


@pytest.mark.parametrize(
    "input_path",
    [
        "../escape.py",
        "../../etc/passwd",
        "a/../../escape.py",
        "../../../../../etc/passwd",
    ],
)
def test_parent_traversal_escapes_rejected(cwd, input_path):
    with pytest.raises(LocalFileAccessViolation) as exc:
        _resolve(input_path, cwd)
    assert "OUTSIDE the project" in str(exc.value)


def test_internal_dotdot_that_resolves_inside_is_allowed(cwd):
    """``a/../b.py`` resolves to ``<cwd>/b.py`` — inside CWD, allowed."""
    r = _resolve("a/../b.py", cwd)
    assert r.relative_to_cwd == "b.py"


def test_dotdot_climbing_back_into_cwd_allowed(cwd):
    """``foo/bar/../baz.py`` resolves to ``<cwd>/foo/baz.py`` — inside CWD."""
    r = _resolve("foo/bar/../baz.py", cwd)
    assert r.relative_to_cwd == "foo/baz.py"


# --- Hostile / malformed inputs ------------------------------------------


@pytest.mark.parametrize(
    "input_path",
    ["", "   ", "\t", "\n", "  \t\n  "],
)
def test_empty_or_whitespace_rejected(cwd, input_path):
    with pytest.raises(LocalFileAccessViolation, match="empty"):
        _resolve(input_path, cwd)


def test_none_rejected(cwd):
    with pytest.raises(LocalFileAccessViolation, match="None"):
        _resolve(None, cwd)  # type: ignore[arg-type]


def test_non_string_rejected(cwd):
    with pytest.raises(LocalFileAccessViolation, match="must be str"):
        _resolve(42, cwd)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "input_path",
    [
        "foo\x00.py",          # null byte
        "foo<bar.py",
        "foo>bar.py",
        "foo|bar.py",
        "foo*.py",
        "foo?.py",
        "with*wildcards/x.py",
    ],
)
def test_dangerous_chars_rejected(cwd, input_path):
    with pytest.raises(LocalFileAccessViolation, match="forbidden characters"):
        _resolve(input_path, cwd)


# --- Whitespace at the edges is trimmed ----------------------------------


def test_leading_trailing_whitespace_trimmed(cwd):
    r = _resolve("   tmp/foo.py   ", cwd)
    assert r.relative_to_cwd == "tmp/foo.py"


# --- Backward-compat wrapper still works ---------------------------------


def test_legacy_wrapper_returns_string_only(cwd):
    """``_validate_path_within_working_directory`` is the old API. Keep it
    behaving the same way (string return, raises on failure) so existing
    callers (list_files, delete_file) keep working."""
    out = _validate_path_within_working_directory("tmp/x.py", cwd)
    assert isinstance(out, str)
    assert out == str(Path(cwd) / "tmp" / "x.py")

    with pytest.raises(LocalFileAccessViolation):
        _validate_path_within_working_directory("../escape.py", cwd)


# ---------------------------------------------------------------------------
# 2. Integration with WriteFileTool — tool result echoes resolved path
# ---------------------------------------------------------------------------


def _ctx(cwd, **parsed):
    return ToolContext(
        user_input="",
        parsed_args=dict(parsed),
        working_directory=str(cwd),
    )


def test_write_file_with_normalized_path_includes_note_in_message(tmp_path):
    """When the LLM sends ``/tmp/foo.py``, the file ends up at
    ``<project>/tmp/foo.py``, AND the tool result message tells the LLM
    so it can never misremember the location on the next turn."""
    tool = WriteFileTool()
    ctx = _ctx(tmp_path, file_path="/tmp/foo.py", content="print('hi')\n")
    result = tool.execute_sync(ctx)

    assert result.is_success
    expected_path = tmp_path / "tmp" / "foo.py"
    assert expected_path.exists()
    assert expected_path.read_text() == "print('hi')\n"

    # Resolved-path metadata must be present and accurate
    assert result.metadata["file_path"] == str(expected_path.resolve())
    assert result.metadata["project_relative_path"] == "tmp/foo.py"
    assert result.metadata["input_path"] == "/tmp/foo.py"
    assert result.metadata["path_normalization_note"] is not None
    assert "leading '/' stripped" in result.metadata["path_normalization_note"]

    # Message must surface the same info to the LLM
    assert "PATH_NORMALIZED" in result.message
    assert str(expected_path.resolve()) in result.message
    assert "tmp/foo.py" in result.message
    assert "/tmp/foo.py" in result.message  # original input echoed


def test_write_file_clean_relative_path_no_note(tmp_path):
    tool = WriteFileTool()
    ctx = _ctx(tmp_path, file_path="src/main.py", content="x = 1\n")
    result = tool.execute_sync(ctx)

    assert result.is_success
    assert result.metadata["path_normalization_note"] is None
    assert "PATH_NORMALIZED" not in result.message


def test_write_file_creates_nested_dirs(tmp_path):
    """When the LLM writes to ``a/b/c/d.py`` and ``a/`` doesn't exist yet,
    the parent dirs must be created automatically — same as before, but
    with the new resolver path."""
    tool = WriteFileTool()
    ctx = _ctx(tmp_path, file_path="a/b/c/d.py", content="x")
    result = tool.execute_sync(ctx)
    assert result.is_success
    assert (tmp_path / "a" / "b" / "c" / "d.py").exists()


def test_write_file_blocks_parent_escape_with_clear_message(tmp_path):
    tool = WriteFileTool()
    ctx = _ctx(tmp_path, file_path="../escape.py", content="x")
    result = tool.execute_sync(ctx)
    assert result.is_error
    assert "OUTSIDE the project" in result.message


def test_write_file_at_prefix_normalized(tmp_path):
    tool = WriteFileTool()
    ctx = _ctx(tmp_path, file_path="@tmp/foo.py", content="hi")
    result = tool.execute_sync(ctx)
    assert result.is_success
    assert (tmp_path / "tmp" / "foo.py").exists()
    assert "'@' prefix stripped" in result.metadata["path_normalization_note"]


# ---------------------------------------------------------------------------
# 3. Integration with ReadFileTool
# ---------------------------------------------------------------------------


def test_read_file_with_normalized_path(tmp_path):
    """LLM sends ``/tmp/foo.py``; the file actually exists at
    ``<project>/tmp/foo.py`` (because that's where write_file put it).
    read_file must find it via the same normalization."""
    target = tmp_path / "tmp" / "foo.py"
    target.parent.mkdir(parents=True)
    target.write_text("hello world\n")

    tool = ReadFileTool()
    ctx = _ctx(tmp_path, file_path="/tmp/foo.py")
    result = tool.execute_sync(ctx)

    assert result.is_success
    assert result.data == "hello world\n"
    assert result.metadata["path_normalization_note"] is not None
    assert "PATH_NORMALIZED" in result.message


def test_read_file_not_found_includes_normalization_hint(tmp_path):
    """If the path was normalized AND the file doesn't exist, the error
    message must surface BOTH facts so the LLM sees ``input was '/tmp/x.py'
    → leading '/' stripped`` and immediately understands what's happening
    instead of trying the same broken path again."""
    tool = ReadFileTool()
    ctx = _ctx(tmp_path, file_path="/tmp/nonexistent.py")
    result = tool.execute_sync(ctx)

    assert result.is_error
    assert "not found" in result.message.lower()
    assert "tmp/nonexistent.py" in result.message
    assert "leading '/' stripped" in result.message


# ---------------------------------------------------------------------------
# 4. Realistic multi-write scenario from the user's transcript (Idea 3)
# ---------------------------------------------------------------------------


def test_multi_write_to_same_subdir_consistent(tmp_path):
    """User Idea 3: write 4 files to ``tmp/calc/``. The 5th write happens
    with the bare name ``__main__.py`` — that should land at the project
    root, NOT at ``tmp/calc/`` (the resolver is stateless), and the tool
    result must clearly say ``project_relative: __main__.py`` so the LLM
    can detect the drift and self-correct."""
    tool = WriteFileTool()
    for name in ["__init__.py", "operations.py", "cli.py", "__main__.py"]:
        ctx = _ctx(tmp_path, file_path=f"tmp/calc/{name}", content=f"# {name}\n")
        result = tool.execute_sync(ctx)
        assert result.is_success
        assert (tmp_path / "tmp" / "calc" / name).exists()
        assert result.metadata["project_relative_path"] == f"tmp/calc/{name}"

    # The "drift" 5th write — bare filename
    ctx = _ctx(tmp_path, file_path="__main__.py", content="# stray\n")
    result = tool.execute_sync(ctx)
    assert result.is_success
    # File ends up at the ROOT, not in tmp/calc/
    assert (tmp_path / "__main__.py").exists()
    # Crucially, the tool result is unambiguous about WHERE it landed
    assert result.metadata["project_relative_path"] == "__main__.py"
    assert result.metadata["file_path"] == str((tmp_path / "__main__.py").resolve())


def test_path_normalization_idempotent_via_file_path(tmp_path):
    """If the LLM uses the absolute file_path from a previous write_file as the
    input to a subsequent read_file, the resolver must accept it as a
    no-normalization no-op (it's already absolute and inside CWD)."""
    tool_w = WriteFileTool()
    res = tool_w.execute_sync(_ctx(tmp_path, file_path="/tmp/x.py", content="ok"))
    assert res.is_success
    resolved = res.metadata["file_path"]

    # Now read using the resolved path the tool just told us — must be
    # accepted without normalization.
    tool_r = ReadFileTool()
    res2 = tool_r.execute_sync(_ctx(tmp_path, file_path=resolved))
    assert res2.is_success
    assert res2.metadata["path_normalization_note"] is None
    assert res2.data == "ok"
