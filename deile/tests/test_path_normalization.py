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

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from deile.tools.base import ToolContext
from deile.tools.file_tools import (
    DeleteFileTool,
    ListFilesTool,
    LocalFileAccessViolation,
    ReadFileTool,
    ResolvedPath,
    WriteFileTool,
    _looks_like_outside_project,
    _resolve_project_path,
    _validate_path_within_working_directory,
)

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
        "foo\x00.py",  # null byte
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


def test_outside_project_violation_points_to_bash_execute(tmp_path):
    """Regression guard for the second-/EVOLVE-run trace: when DEILE was
    launched from ``deilebot/`` (a sibling clone of the deile parent repo) and
    asked to read templates at ``../.github/ISSUE_TEMPLATE/``, the path
    resolver rejected it with a generic "OUTSIDE the project" message that
    didn't tell the LLM HOW to recover. The model then looped on the same
    broken call.

    The fix surfaces, inside the LocalFileAccessViolation message, an
    explicit pointer to ``bash_execute`` — the only tool in DEILE that has
    no working-directory sandbox and CAN access parent / sibling repos.
    """
    with pytest.raises(LocalFileAccessViolation) as exc:
        _resolve_project_path("../.github/ISSUE_TEMPLATE/", str(tmp_path))
    msg = str(exc.value)
    assert "OUTSIDE the project" in msg
    assert "bash_execute" in msg
    # Must include a concrete example so the LLM can copy-paste — `ls` or `cat`
    assert "`ls " in msg or "`cat " in msg
    assert "no working-directory sandbox" in msg


# ---------------------------------------------------------------------------
# 2b. _looks_like_outside_project heuristic — pure-string detector that
#     decides whether to attach a bash_execute hint to "Path not found"
#     messages from list_files / read_file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/Users/x/foo.txt",
        "/etc/passwd",
        "/tmp/foo",
        "..",
        "../sibling/file.py",
        "../../parent/of/parent.txt",
        "~",
        "~/foo.py",
    ],
)
def test_looks_like_outside_project_true(path):
    assert _looks_like_outside_project(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "foo.py",
        "src/main.py",
        "./local.py",
        "a/../b.py",  # internal .. doesn't trigger heuristic
        ".github/ISSUE_TEMPLATE/",
        "",
        "   ",
    ],
)
def test_looks_like_outside_project_false(path):
    assert _looks_like_outside_project(path) is False


def test_looks_like_outside_project_handles_non_string():
    assert _looks_like_outside_project(None) is False
    assert _looks_like_outside_project(42) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2c. ListFilesTool — surface normalization note + bash_execute hint
# ---------------------------------------------------------------------------


def test_list_files_not_found_for_normalized_absolute_path_hints_bash(tmp_path):
    """Direct repro of the second-run failure mode.

    User launches DEILE from a subdir, then asks it to read
    ``/Users/.../parent_repo/.github/ISSUE_TEMPLATE/``. The resolver strips
    the leading ``/`` (treating it as a project-relative typo), the
    resulting path doesn't exist inside CWD, and ``list_files`` returns
    "Path not found".

    Before the fix: the LLM saw only "Path not found: /Users/...", didn't
    realize the path had been mangled, and looped on the same call.

    After the fix: the message includes (a) the normalization note so the
    LLM SEES the slash was stripped, and (b) an explicit pointer to
    ``bash_execute`` so it knows exactly which tool to use instead.
    """
    tool = ListFilesTool()
    nonexistent_abs = "/some/system/path/that/does/not/exist/anywhere"
    ctx = _ctx(tmp_path, path=nonexistent_abs)
    result = tool.execute_sync(ctx)

    assert result.is_error
    assert "Path not found" in result.message
    # (a) Normalization note must be visible
    assert "leading '/' stripped" in result.message
    # (b) bash_execute hint must be visible
    assert "bash_execute" in result.message


def test_list_files_not_found_for_parent_relative_path_hints_bash(tmp_path):
    """Same idea but for ``../parent_repo/...`` — the resolver raises
    LocalFileAccessViolation here. The list_files tool catches it and
    surfaces the violation message, which the resolver enriched with the
    bash_execute hint.
    """
    tool = ListFilesTool()
    ctx = _ctx(tmp_path, path="../escape/.github")
    result = tool.execute_sync(ctx)

    assert result.is_error
    assert "OUTSIDE the project" in result.message
    assert "bash_execute" in result.message


def test_list_files_not_found_for_clean_relative_path_no_bash_hint(tmp_path):
    """Negative case: when the LLM asks for a non-existent project-relative
    path (a typo like ``misspelled_dir/``), we still want a clear "Path
    not found" — but we should NOT spam the bash_execute hint, because
    the path is conceptually correct (project-scoped) and just doesn't
    exist yet. Adding the hint here would mislead the LLM into reaching
    for bash when ``write_file`` / ``mkdir`` is the right answer.
    """
    tool = ListFilesTool()
    ctx = _ctx(tmp_path, path="misspelled_dir/")
    result = tool.execute_sync(ctx)

    assert result.is_error
    assert "Path not found" in result.message
    assert "bash_execute" not in result.message


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


# ---------------------------------------------------------------------------
# 5. Tier-1: bash_execute hint consistency across path tools (issue #149)
#
# Every path-tool must include "bash_execute" in its error message when
# the supplied path looks like it targets outside the project CWD.  Two
# flavours are tested per tool:
#   - out_of_cwd_absolute: leading '/' that normalises to a non-existent
#     project-relative path (the file was never created inside the project).
#   - parent_relative: '..' that escapes the project root →
#     LocalFileAccessViolation (message already enriched in PR #151).
# ---------------------------------------------------------------------------


# --- read_file ---------------------------------------------------------------


def test_read_file_out_of_cwd_absolute_hints_bash(tmp_path):
    """read_file(path='/tmp/nonexistent.py') → file not found after
    normalization → error message must contain 'bash_execute'."""
    tool = ReadFileTool()
    ctx = _ctx(tmp_path, file_path="/tmp/nonexistent_file_xyz.py")
    result = tool.execute_sync(ctx)

    assert result.is_error
    assert "not found" in result.message.lower()
    assert "bash_execute" in result.message


def test_read_file_parent_relative_hints_bash(tmp_path):
    """read_file(path='../../etc/passwd') → LocalFileAccessViolation →
    message (enriched by resolver) must contain 'bash_execute'."""
    tool = ReadFileTool()
    ctx = _ctx(tmp_path, file_path="../../etc/passwd")
    result = tool.execute_sync(ctx)

    assert result.is_error
    assert "OUTSIDE" in result.message or "outside" in result.message.lower()
    assert "bash_execute" in result.message


# --- write_file ---------------------------------------------------------------


def test_write_file_parent_relative_hints_bash(tmp_path):
    """write_file(path='../../etc/crontab') → LocalFileAccessViolation →
    message must contain 'bash_execute'."""
    tool = WriteFileTool()
    ctx = _ctx(tmp_path, file_path="../../etc/crontab", content="bad")
    result = tool.execute_sync(ctx)

    assert result.is_error
    assert "OUTSIDE" in result.message or "outside" in result.message.lower()
    assert "bash_execute" in result.message


def test_write_file_out_of_cwd_absolute_path_normalized_note_present(tmp_path):
    """write_file(path='/tmp/foo.py') succeeds (normalised to project-relative);
    the success message must include the PATH_NORMALIZED warning so the LLM
    knows the resolved path, not the original input, is what was written."""
    tool = WriteFileTool()
    ctx = _ctx(tmp_path, file_path="/tmp/outside_hint_test.py", content="# ok")
    result = tool.execute_sync(ctx)

    assert result.is_success
    assert "PATH_NORMALIZED" in result.message


# --- delete_file -------------------------------------------------------------


def test_delete_file_out_of_cwd_absolute_hints_bash(tmp_path):
    """delete_file(path='/tmp/phantom.py') → file not found after normalization
    → error message must contain 'bash_execute'."""
    tool = DeleteFileTool()
    ctx = _ctx(tmp_path, file_path="/tmp/phantom_delete_test.py")
    result = tool.execute_sync(ctx)

    assert result.is_error
    assert "not found" in result.message.lower()
    assert "bash_execute" in result.message


def test_delete_file_parent_relative_hints_bash(tmp_path):
    """delete_file(path='../../important.conf') → LocalFileAccessViolation →
    message must contain 'bash_execute'."""
    tool = DeleteFileTool()
    ctx = _ctx(tmp_path, file_path="../../important_delete_test.conf", force=True)
    result = tool.execute_sync(ctx)

    assert result.is_error
    assert "OUTSIDE" in result.message or "outside" in result.message.lower()
    assert "bash_execute" in result.message


# ---------------------------------------------------------------------------
# 6. Windows-specific platform branches (issue #283)
#
# `_path_resolution.py` has two Windows-conditional code paths that the
# Linux CI never executes:
#   * `_PYTHON_LAUNCHER = "python" if sys.platform == "win32" else "python3"`
#     → drives every post-write validation hint (`.py`, `.json`, `.yaml`).
#   * `_WINDOWS_DRIVE_RE` → strips `C:\foo`, `D:/bar`, etc. so a path the
#     LLM copy-pasted from a Windows transcript still resolves correctly.
#
# These tests mock `sys.platform`, reload the module so the constant is
# rebuilt, and assert the hint command + regex behave correctly per-platform.
# ---------------------------------------------------------------------------


def _reload_path_resolution_with_platform(target_platform: str):
    """Reimport `_path_resolution` under a forced `sys.platform`.

    `_PYTHON_LAUNCHER` is captured at import time, so changing
    `sys.platform` after the fact would not affect it. The test patches
    `sys.platform`, drops the cached module, and reimports — then yields the
    fresh module to the caller. Cleanup happens in a `finally` block so the
    rest of the suite sees the real module afterwards.
    """
    sys.modules.pop("deile.tools._path_resolution", None)
    try:
        with patch.object(sys, "platform", target_platform):
            return importlib.import_module("deile.tools._path_resolution")
    finally:
        # Restore the real module so subsequent tests use the real impl.
        sys.modules.pop("deile.tools._path_resolution", None)
        importlib.import_module("deile.tools._path_resolution")


def test_python_launcher_is_python_on_windows():
    module = _reload_path_resolution_with_platform("win32")
    assert module._PYTHON_LAUNCHER == "python"


def test_python_launcher_is_python3_on_linux():
    module = _reload_path_resolution_with_platform("linux")
    assert module._PYTHON_LAUNCHER == "python3"


def test_python_launcher_is_python3_on_macos():
    module = _reload_path_resolution_with_platform("darwin")
    assert module._PYTHON_LAUNCHER == "python3"


def test_post_write_hint_uses_python_on_windows():
    """`.py` files on Windows get `python -m py_compile`, not `python3`."""
    module = _reload_path_resolution_with_platform("win32")

    hint = module._post_write_validation_hint("script.py")
    assert hint is not None
    assert hint["kind"] == "python_syntax"
    assert hint["command"] == "python -m py_compile script.py"
    assert "python3" not in hint["command"]


def test_post_write_hint_uses_python3_on_linux():
    """Negative control: Linux still uses `python3`."""
    module = _reload_path_resolution_with_platform("linux")

    hint = module._post_write_validation_hint("script.py")
    assert hint is not None
    assert hint["command"] == "python3 -m py_compile script.py"


def test_post_write_json_hint_uses_python_launcher_on_windows():
    """JSON and YAML hints inherit `_PYTHON_LAUNCHER` too — verify Windows."""
    module = _reload_path_resolution_with_platform("win32")

    json_hint = module._post_write_validation_hint("data.json")
    assert json_hint is not None
    assert json_hint["command"].startswith("python ")
    assert "python3" not in json_hint["command"]

    yaml_hint = module._post_write_validation_hint("config.yaml")
    assert yaml_hint is not None
    assert yaml_hint["command"].startswith("python ")


@pytest.mark.parametrize(
    "drive_path, expected_remainder",
    [
        ("C:\\Users\\foo.py", "Users\\foo.py"),
        ("D:/data/bar.py", "data/bar.py"),
        ("c:\\\\baz.py", "baz.py"),
        ("Z:/x.py", "x.py"),
        ("a:/y", "y"),
    ],
)
def test_windows_drive_regex_strips_drive_prefix(drive_path, expected_remainder):
    """`_WINDOWS_DRIVE_RE` matches `<letter>:` followed by one+ slashes/backslashes."""
    from deile.tools._path_resolution import _WINDOWS_DRIVE_RE

    match = _WINDOWS_DRIVE_RE.match(drive_path)
    assert match is not None, f"expected regex to match {drive_path!r}"

    stripped = _WINDOWS_DRIVE_RE.sub("", drive_path)
    assert stripped == expected_remainder


@pytest.mark.parametrize(
    "non_drive_path",
    [
        "foo.py",
        "/tmp/foo",
        "src/main.py",
        "@foo.py",
        "1:invalid",  # leading digit, not a drive letter
        ":",  # no letter
    ],
)
def test_windows_drive_regex_does_not_match_non_drive_paths(non_drive_path):
    """Negative control: paths that aren't Windows drives must not match."""
    from deile.tools._path_resolution import _WINDOWS_DRIVE_RE

    assert _WINDOWS_DRIVE_RE.match(non_drive_path) is None
