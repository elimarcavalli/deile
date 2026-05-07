"""Tests for exception logging in git_tool.py and execution_tools.py (issues #110, #114)."""

from unittest.mock import MagicMock, patch

import pytest

from deile.tools.base import ToolContext
from deile.tools.execution_tools import EnhancedExecutionTool, PTYSession
from deile.tools.git_tool import GitTool


def _make_context(**kwargs):
    ctx = MagicMock(spec=ToolContext)
    ctx.parsed_args = kwargs
    ctx.working_directory = "/tmp"
    return ctx


# ---------------------------------------------------------------------------
# git_tool.py — remote fetch in _git_status
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_git_status_remote_fetch_logs_warning():
    mock_repo = MagicMock()
    mock_repo.active_branch.name = "main"
    mock_repo.head.commit.hexsha = "abcd1234"
    mock_repo.index.diff.return_value = []
    mock_repo.untracked_files = []
    mock_origin = MagicMock()
    mock_origin.fetch.side_effect = OSError("network unreachable")
    mock_repo.remote.return_value = mock_origin

    tool = GitTool()

    with patch("deile.tools.git_tool.logger") as mock_logger:
        result = tool._git_status(mock_repo)

    assert result["success"] is True
    mock_logger.warning.assert_called_once()
    assert "network unreachable" in str(mock_logger.warning.call_args)
    assert mock_logger.warning.call_args[1].get("exc_info") is True


# ---------------------------------------------------------------------------
# git_tool.py — file diff in _git_diff
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_git_diff_file_logs_warning():
    mock_repo = MagicMock()
    mock_repo.git.diff.side_effect = OSError("path not found")

    tool = GitTool()

    with patch("deile.tools.git_tool.logger") as mock_logger:
        result = tool._git_diff(mock_repo, {"files": ["missing_file.py"]})

    assert result["success"] is True
    assert "File not found or no diff" in result["output"]
    mock_logger.warning.assert_called_once()
    assert "path not found" in str(mock_logger.warning.call_args)
    assert mock_logger.warning.call_args[1].get("exc_info") is True


# ---------------------------------------------------------------------------
# git_tool.py — remote fetch before push in _git_push
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_git_push_precheck_logs_warning():
    mock_repo = MagicMock()
    mock_repo.active_branch.name = "feature"
    mock_remote = MagicMock()
    mock_remote.fetch.side_effect = OSError("connection refused")
    push_info = MagicMock()
    push_info.summary = "pushed ok"
    mock_remote.push.return_value = [push_info]
    mock_repo.remote.return_value = mock_remote

    tool = GitTool()

    with patch("deile.tools.git_tool.logger") as mock_logger:
        result = tool._git_push(mock_repo, {"remote": "origin", "force": False, "dry_run": False})

    assert result["success"] is True
    mock_logger.warning.assert_called_once()
    assert "connection refused" in str(mock_logger.warning.call_args)
    assert mock_logger.warning.call_args[1].get("exc_info") is True


# ---------------------------------------------------------------------------
# execution_tools.py — session cleanup in _execute_interactive
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_execute_interactive_cleanup_logs_warning():
    tool = EnhancedExecutionTool()

    broken_session = MagicMock()
    broken_session.start.return_value = True
    broken_session.write_input.return_value = None
    broken_session.read_output.return_value = ""
    broken_session.read_errors.return_value = ""
    broken_session.get_exit_code.return_value = None
    broken_session.is_alive.side_effect = RuntimeError("PTY exploded")
    broken_session.terminate.side_effect = RuntimeError("terminate failed")

    ctx = _make_context(command="echo hi", interactive=True, timeout=1, env={}, input="")

    with patch("deile.tools.execution_tools.PTYSession", return_value=broken_session):
        with patch("deile.tools.execution_tools.logger") as mock_logger:
            tool._execute_interactive("echo hi", ctx, 1, {}, "")

    mock_logger.warning.assert_called()
    warning_messages = " ".join(str(c) for c in mock_logger.warning.call_args_list)
    assert "terminate session" in warning_messages.lower()
    assert mock_logger.warning.call_args[1].get("exc_info") is True


# ---------------------------------------------------------------------------
# execution_tools.py — outer except in _execute_interactive logs error
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_execute_interactive_outer_except_logs_error_with_exc_info():
    tool = EnhancedExecutionTool()

    session = MagicMock()
    session.start.return_value = True
    session.write_input.return_value = None
    session.read_output.return_value = ""
    session.read_errors.return_value = ""
    session.get_exit_code.return_value = None
    session.is_alive.side_effect = RuntimeError("session crashed")
    session.terminate.return_value = True  # cleanup succeeds — no warning expected

    ctx = _make_context(command="echo hi", interactive=True, timeout=1, env={}, input="")

    with patch("deile.tools.execution_tools.PTYSession", return_value=session):
        with patch("deile.tools.execution_tools.logger") as mock_logger:
            tool._execute_interactive("echo hi", ctx, 1, {}, "")

    mock_logger.error.assert_called()
    error_call_kwargs = mock_logger.error.call_args[1]
    assert error_call_kwargs.get("exc_info") is True
    mock_logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# execution_tools.py — PTY read in _read_output_windows
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_read_output_windows_pty_error_logs_warning_with_exc_info():
    session = PTYSession("echo test", "/tmp")
    session.is_running = True
    session.pty_process = MagicMock()
    session.pty_process.read.side_effect = OSError("PTY read failed")
    session._cleanup_windows = MagicMock()

    with patch("deile.tools.execution_tools.logger") as mock_logger:
        session._read_output_windows()

    mock_logger.warning.assert_called_once()
    assert "PTY read failed" in str(mock_logger.warning.call_args)
    assert mock_logger.warning.call_args[1].get("exc_info") is True
