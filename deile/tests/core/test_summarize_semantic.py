"""Tests for tool-name aware ``summarize`` / ``semantic_summary``.

These functions decide what shows up after the ``⎿`` marker in the
streaming UI for each tool call. Regressions here directly degrade the
operator's ability to see what tools just did.
"""

from __future__ import annotations

from deile.core.tool_result_summary import (
    SUMMARY_MAX_CHARS,
    semantic_summary,
    summarize,
)
from deile.tools.base import ToolResult, ToolStatus


def _ok(tool: str, *, data=None, message="", **meta) -> ToolResult:
    return ToolResult(
        status=ToolStatus.SUCCESS,
        data=data,
        message=message,
        metadata={"function_name": tool, **meta},
    )


def test_bash_execute_dict_data_renders_exit_and_time():
    """``bash_tool.BashTool`` packs the full dict in data — must NOT leak Python repr."""
    result = _ok(
        "bash_execute",
        data={
            "exit_code": 0,
            "stdout": "hello world\nline 2",
            "stderr": "",
            "execution_time": 0.0134,
        },
        message="exited 0",
    )
    out = summarize(result)
    assert "exit 0" in out
    assert "13ms" in out
    assert "hello world" in out
    # Python dict repr must NOT leak.
    assert "'exit_code'" not in out
    assert "{'exit_code" not in out


def test_bash_execute_failure_shows_stderr_first_line():
    result = ToolResult(
        status=ToolStatus.ERROR,
        data={
            "exit_code": 1,
            "stdout": "",
            "stderr": "fatal: bad path\nmore details",
            "execution_time": 0.05,
        },
        message="exited 1",
        metadata={"function_name": "bash_execute", "exit_code": 1},
    )
    out = summarize(result)
    # Error path uses the message, not the semantic renderer.
    assert "error:" in out


def test_python_execute_dict_renders_like_bash():
    result = _ok(
        "python_execute",
        data={"exit_code": 0, "stdout": "42\n", "stderr": "", "execution_time": 0.025},
    )
    out = summarize(result)
    assert "exit 0" in out
    assert "25ms" in out
    assert "42" in out


def test_read_file_renders_bytes_and_lines():
    content = "TOTAL = 100\ndef increment(): return TOTAL + 1\n"
    result = _ok("read_file", data=content, file_size=len(content))
    out = summarize(result)
    assert "bytes" in out
    assert "lines" in out or "line" in out
    # File content must NOT be dumped raw (would break newlines into spaces).
    assert "def increment" not in out


def test_read_file_single_line_uses_singular():
    result = _ok("read_file", data="x = 1\n", file_size=6)
    out = summarize(result)
    assert "1 line" in out
    # Strict singular: no plural with same prefix.
    assert "1 lines" not in out


def test_write_file_shows_bytes_and_relative_path():
    result = _ok(
        "write_file",
        data="/abs/path/to/foo.py",
        message="wrote 42 bytes",
        content_length=42,
        project_relative_path="foo.py",
    )
    out = summarize(result)
    assert "42 bytes written" in out
    assert "foo.py" in out
    # Absolute path must NOT leak.
    assert "/abs/path/" not in out


def test_list_files_renders_count_and_names():
    result = _ok(
        "list_files",
        data=[
            {"name": "__pycache__", "type": "directory"},
            {"name": "counter.py", "type": "file"},
            {"name": "hello.py", "type": "file"},
        ],
        total_items=3,
    )
    out = summarize(result)
    assert "3 entries" in out
    assert "__pycache__/" in out  # directory has trailing slash
    assert "counter.py" in out
    # No Python list repr.
    assert "[{'name'" not in out
    assert "'modified':" not in out


def test_list_files_more_than_three_shows_overflow():
    result = _ok(
        "list_files",
        data=[{"name": f"f{i}.py", "type": "file"} for i in range(5)],
        total_items=5,
    )
    out = summarize(result)
    assert "5 entries" in out
    assert "+2" in out  # "… +2" suffix


def test_list_files_single_uses_entry_singular():
    result = _ok(
        "list_files",
        data=[{"name": "only.txt", "type": "file"}],
        total_items=1,
    )
    out = summarize(result)
    assert "1 entry" in out
    assert "1 entries" not in out


def test_delete_file_cleans_verbose_prefix():
    result = _ok(
        "delete_file",
        message="Successfully deleted directory: test-your-might/ui-check",
    )
    out = summarize(result)
    assert "deleted dir" in out
    assert "Successfully deleted directory:" not in out


def test_unknown_tool_falls_back_to_default():
    """For tools we don't special-case, the original behaviour must hold."""
    result = _ok("some_random_tool", data="hello", message="ok")
    out = summarize(result)
    # Falls back to str(data) — should not crash and should contain the data.
    assert "hello" in out


def test_no_function_name_falls_back_to_default():
    """ToolResult without metadata.function_name must still summarize."""
    result = ToolResult(
        status=ToolStatus.SUCCESS,
        data="payload",
        message="ok",
        metadata={},
    )
    out = summarize(result)
    assert "payload" in out


def test_semantic_summary_returns_none_for_unknown_tool():
    """The semantic helper itself returns None for unknown tools (no fallback)."""
    result = _ok("__nope__", data="x")
    assert semantic_summary("__nope__", result) is None


# ── Lock-in tests for truncation, error fallback, and edge cases ─────────────


def test_truncation_exact_boundary_is_not_truncated():
    """A body exactly ``SUMMARY_MAX_CHARS`` long must survive verbatim."""
    body = "a" * SUMMARY_MAX_CHARS
    result = ToolResult(
        status=ToolStatus.SUCCESS,
        data=body,
        message="",
        metadata={},
    )
    out = summarize(result)
    assert len(out) == SUMMARY_MAX_CHARS
    assert not out.endswith("…")
    assert out == body


def test_truncation_one_over_boundary_gets_ellipsis():
    """A body of ``SUMMARY_MAX_CHARS + 1`` is truncated to MAX with ellipsis tail."""
    body = "a" * (SUMMARY_MAX_CHARS + 1)
    result = ToolResult(
        status=ToolStatus.SUCCESS,
        data=body,
        message="",
        metadata={},
    )
    out = summarize(result)
    assert len(out) == SUMMARY_MAX_CHARS
    assert out.endswith("…")


def test_error_collapses_newlines_from_error_attribute():
    """Newlines/CR in ``result.error`` (with empty message) must collapse to spaces."""
    result = ToolResult(
        status=ToolStatus.ERROR,
        data=None,
        message="",
        error=Exception("line1\nline2\rline3"),
        metadata={},
    )
    out = summarize(result)
    assert "\n" not in out
    assert "\r" not in out
    assert "line1 line2 line3" in out
    assert out.startswith("error: ")


def test_error_fallback_uses_error_when_message_empty():
    """Empty ``message`` falls through to ``error`` text on the ERROR path."""
    result = ToolResult(
        status=ToolStatus.ERROR,
        data=None,
        message="",
        error="boom",
        metadata={},
    )
    out = summarize(result)
    assert out == "error: boom"


def test_error_fallback_uses_error_when_message_none():
    """``message=None`` (falsy) also falls through to ``error``."""
    result = ToolResult(
        status=ToolStatus.ERROR,
        data=None,
        message=None,
        error="boom",
        metadata={},
    )
    out = summarize(result)
    assert out == "error: boom"


def test_write_file_prefers_project_relative_path_over_input_path():
    """When both present, ``project_relative_path`` wins."""
    result = ToolResult(
        status=ToolStatus.SUCCESS,
        data=None,
        message="",
        metadata={
            "function_name": "write_file",
            "content_length": 50,
            "project_relative_path": "a.py",
            "input_path": "/abs/b.py",
        },
    )
    out = summarize(result)
    assert "50 bytes written" in out
    assert "a.py" in out
    assert "/abs/b.py" not in out


def test_write_file_falls_back_to_input_path():
    """With only ``input_path`` set, that path is rendered."""
    result = ToolResult(
        status=ToolStatus.SUCCESS,
        data=None,
        message="",
        metadata={
            "function_name": "write_file",
            "content_length": 50,
            "input_path": "/abs/b.py",
        },
    )
    out = summarize(result)
    assert "50 bytes written" in out
    assert "/abs/b.py" in out


def test_write_file_no_path_renders_bytes_only():
    """With no path keys, the renderer must not crash and reports bytes only."""
    result = ToolResult(
        status=ToolStatus.SUCCESS,
        data=None,
        message="",
        metadata={"function_name": "write_file", "content_length": 50},
    )
    out = summarize(result)
    assert out == "50 bytes written"


def test_write_file_missing_length_falls_through_to_default():
    """No ``content_length`` → semantic_summary returns None → default fallback."""
    result = ToolResult(
        status=ToolStatus.SUCCESS,
        data="datavalue",
        message="msg",
        metadata={"function_name": "write_file"},
    )
    out = summarize(result)
    # Falls through to ``str(data)`` since semantic_summary returned None.
    assert out == "datavalue"


def test_read_file_bytes_data_does_not_crash():
    """``data`` as ``bytes`` (no ``file_size`` meta) must not raise.

    Current behaviour: ``semantic_summary`` returns None (no ``file_size``
    and ``isinstance(data, str)`` is False), so the renderer falls back to
    ``str(data)`` — which yields the Python ``b'...'`` repr. This test
    locks in that contract; if future work renders binary more cleanly,
    update the assertion in the same change.
    """
    result = ToolResult(
        status=ToolStatus.SUCCESS,
        data=b"binary data",
        message="",
        metadata={"function_name": "read_file"},
    )
    out = summarize(result)
    # Locked contract: bytes → str(bytes) repr.
    assert out == "b'binary data'"
