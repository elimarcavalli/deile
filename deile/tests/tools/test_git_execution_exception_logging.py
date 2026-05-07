"""Tests for exception logging in git_tool.py and execution_tools.py (issue #110)."""

from unittest.mock import MagicMock, patch

import pytest

from deile.tools.base import ToolContext
from deile.tools.execution_tools import EnhancedExecutionTool
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
    mock_origin.fetch.side_effect = RuntimeError("network unreachable")
    mock_repo.remote.return_value = mock_origin

    tool = GitTool()

    with patch("deile.tools.git_tool.logger") as mock_logger:
        result = tool._git_status(mock_repo)

    assert result["success"] is True
    mock_logger.warning.assert_called_once()
    assert "network unreachable" in str(mock_logger.warning.call_args)


# ---------------------------------------------------------------------------
# git_tool.py — file diff in _git_diff
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_git_diff_file_logs_warning():
    mock_repo = MagicMock()
    mock_repo.git.diff.side_effect = RuntimeError("path not found")

    tool = GitTool()

    with patch("deile.tools.git_tool.logger") as mock_logger:
        result = tool._git_diff(mock_repo, {"files": ["missing_file.py"]})

    assert result["success"] is True
    assert "File not found or no diff" in result["output"]
    mock_logger.warning.assert_called_once()
    assert "path not found" in str(mock_logger.warning.call_args)


# ---------------------------------------------------------------------------
# git_tool.py — remote fetch before push in _git_push
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_git_push_precheck_logs_warning():
    mock_repo = MagicMock()
    mock_repo.active_branch.name = "feature"
    mock_remote = MagicMock()
    mock_remote.fetch.side_effect = RuntimeError("connection refused")
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
    assert any(keyword in warning_messages.lower() for keyword in ["terminate", "cleanup", "session"])
