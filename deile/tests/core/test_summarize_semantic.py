"""Tests for tool-name aware ``summarize`` / ``semantic_summary``.

These functions decide what shows up after the ``⎿`` marker in the
streaming UI for each tool call. Regressions here directly degrade the
operator's ability to see what tools just did.
"""

from __future__ import annotations

from deile.core.tool_result_summary import semantic_summary, summarize
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
        data={"exit_code": 1, "stdout": "", "stderr": "fatal: bad path\nmore details", "execution_time": 0.05},
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
