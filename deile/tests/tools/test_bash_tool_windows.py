"""Tests for the Windows-specific branches of ``BashExecuteTool``.

The CI runs on Linux so the ``self.platform == 'Windows'`` paths in
``deile/tools/bash_tool.py`` are never exercised. A regression there only
surfaces when a Windows user runs DEILE in production. These tests mock
``platform.system()`` / ``sys.platform`` and assert each Windows branch:

* ``_execute_with_subprocess`` shells out via ``cmd.exe /c`` (not
  ``/bin/bash -c``).
* ``_prepare_environment`` adds Windows-specific PATH entries instead of
  the POSIX set, **and** scrubs them via ``os.path.exists`` so the test
  is deterministic on the Linux CI runner.
* ``execute_sync`` skips the PTY path on Windows even when ``use_pty=True``
  is requested.
* The ``try: import pty, select, tty`` block at module top is tolerant of
  Windows where those modules don't exist.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from deile.tools.base import ToolContext


def _make_tool() -> Any:
    """Build a ``BashExecuteTool`` after forcing ``platform.system()`` to Windows.

    ``self.platform`` is captured in ``__init__`` from ``platform.system()``, so
    the patch has to be active when the constructor runs — patching after the
    fact would leave ``self.platform == 'Darwin'`` on macOS / ``'Linux'`` on
    CI and silently miss every assertion below.
    """
    with patch("deile.tools.bash_tool.platform.system", return_value="Windows"):
        from deile.tools.bash_tool import BashExecuteTool

        return BashExecuteTool()


def _make_ctx(command: str, working_dir: Path, **extra: Any) -> ToolContext:
    parsed: Dict[str, Any] = {"command": command, "working_directory": str(working_dir)}
    parsed.update(extra)
    return ToolContext(
        user_input="",
        parsed_args=parsed,
        working_directory=str(working_dir),
    )


@pytest.mark.bash
@pytest.mark.unit
class TestBashExecuteSubprocessWindows:
    """``_execute_with_subprocess`` must select ``cmd.exe`` on Windows."""

    def test_subprocess_uses_cmd_exe_on_windows(self, tmp_path: Path) -> None:
        tool = _make_tool()
        assert tool.platform == "Windows"

        fake_result = MagicMock(stdout="hello\n", stderr="", returncode=0)
        with patch(
            "deile.tools.bash_tool.subprocess.run", return_value=fake_result
        ) as mock_run:
            stdout, stderr, exit_code, pty_used = tool._execute_with_subprocess(
                command="echo hello",
                working_dir=tmp_path,
                env={},
                timeout=5.0,
            )

        assert stdout == "hello\n"
        assert stderr == ""
        assert exit_code == 0
        assert pty_used is False

        mock_run.assert_called_once()
        # Positional args[0] is the shell command list — cmd.exe /c <command>.
        shell_cmd = mock_run.call_args[0][0]
        assert shell_cmd == ["cmd.exe", "/c", "echo hello"]

    def test_subprocess_uses_bash_on_posix_for_contrast(self, tmp_path: Path) -> None:
        """Negative control: make sure the Windows assertion above is meaningful.

        If the branch logic ever inverts, the contrast test fails first and
        points at the regression.
        """
        with patch("deile.tools.bash_tool.platform.system", return_value="Linux"):
            from deile.tools.bash_tool import BashExecuteTool

            tool = BashExecuteTool()

        fake_result = MagicMock(stdout="", stderr="", returncode=0)
        with patch(
            "deile.tools.bash_tool.subprocess.run", return_value=fake_result
        ) as mock_run:
            tool._execute_with_subprocess(
                command="echo hi",
                working_dir=tmp_path,
                env={},
                timeout=5.0,
            )

        shell_cmd = mock_run.call_args[0][0]
        assert shell_cmd == ["/bin/bash", "-c", "echo hi"]

    def test_subprocess_timeout_raises_timeout_error(self, tmp_path: Path) -> None:
        """``subprocess.TimeoutExpired`` on Windows still surfaces as ``TimeoutError``."""
        import subprocess as _subprocess

        tool = _make_tool()
        with patch(
            "deile.tools.bash_tool.subprocess.run",
            side_effect=_subprocess.TimeoutExpired(cmd="cmd.exe", timeout=1),
        ):
            with pytest.raises(TimeoutError, match="timed out"):
                tool._execute_with_subprocess(
                    command="echo hi",
                    working_dir=tmp_path,
                    env={},
                    timeout=1.0,
                )


@pytest.mark.bash
@pytest.mark.unit
class TestPrepareEnvironmentWindows:
    """``_prepare_environment`` must add Windows PATH entries on Windows.

    The Linux CI runner doesn't have ``C:\\Windows\\System32`` etc., so we
    also patch ``os.path.exists`` to return ``True`` for the Windows paths
    — otherwise the function's existence guard skips every candidate and
    the assertion becomes vacuous.
    """

    WINDOWS_PATHS = {
        r"C:\Windows\System32",
        r"C:\Windows",
        r"C:\Program Files\Git\bin",
        r"C:\Program Files\Git\cmd",
    }

    def test_windows_paths_prepended(self, tmp_path: Path) -> None:
        tool = _make_tool()

        with (
            patch.dict("os.environ", {"PATH": "/existing/path"}, clear=True),
            patch("deile.tools.bash_tool.os.path.exists", return_value=True),
        ):
            env = tool._prepare_environment(base_env=None, working_dir=tmp_path)

        # Every Windows path must appear in the resulting PATH string.
        for win_path in self.WINDOWS_PATHS:
            assert (
                win_path in env["PATH"]
            ), f"expected {win_path!r} in PATH but got {env['PATH']!r}"

        # PWD always set to the working directory.
        assert env["PWD"] == str(tmp_path)

    def test_posix_paths_used_on_linux(self, tmp_path: Path) -> None:
        """Negative control — make sure the Windows assertion isn't picking
        up POSIX paths by accident."""
        with patch("deile.tools.bash_tool.platform.system", return_value="Linux"):
            from deile.tools.bash_tool import BashExecuteTool

            tool = BashExecuteTool()

        with (
            patch.dict("os.environ", {"PATH": "/x"}, clear=True),
            patch("deile.tools.bash_tool.os.path.exists", return_value=True),
        ):
            env = tool._prepare_environment(base_env=None, working_dir=tmp_path)

        # POSIX paths must be present; Windows paths must be absent.
        for posix_path in ["/usr/local/bin", "/usr/bin", "/bin"]:
            assert posix_path in env["PATH"]
        for win_path in self.WINDOWS_PATHS:
            assert win_path not in env["PATH"]


@pytest.mark.bash
@pytest.mark.unit
class TestExecuteSyncSkipsPtyOnWindows:
    """``execute_sync`` must NOT take the PTY branch on Windows.

    The relevant guard is:
        ``if should_use_pty and not sandbox and self.platform != 'Windows':``
    On Windows the PTY branch is bypassed even when ``use_pty=True``.
    """

    def test_pty_branch_skipped_on_windows(self, tmp_path: Path) -> None:
        tool = _make_tool()

        # Force ``_should_use_pty`` to return True so the only thing keeping
        # us off the PTY path is the Windows check.
        with (
            patch.object(tool, "_should_use_pty", return_value=True),
            patch.object(tool, "_execute_with_pty_unix") as mock_pty,
            patch.object(
                tool,
                "_execute_with_subprocess",
                return_value=("ok", "", 0, False),
            ) as mock_subprocess,
            patch("deile.tools.bash_tool.get_settings") as mock_settings,
        ):
            mock_settings.return_value.sandbox_code_execution = False

            ctx = _make_ctx("echo hi", tmp_path, use_pty=True)
            result = tool.execute_sync(ctx)

        # PTY path must NEVER be taken on Windows.
        mock_pty.assert_not_called()
        # Subprocess path must be taken instead.
        mock_subprocess.assert_called_once()
        assert result.is_success
        assert result.data["pty_used"] is False

    def test_sandbox_forces_subprocess_on_windows(self, tmp_path: Path) -> None:
        """Already-covered POSIX invariant — verify it also holds on Windows."""
        tool = _make_tool()

        with (
            patch.object(tool, "_should_use_pty", return_value=True),
            patch.object(tool, "_execute_with_pty_unix") as mock_pty,
            patch.object(
                tool,
                "_execute_with_subprocess",
                return_value=("ok", "", 0, False),
            ) as mock_subprocess,
            patch("deile.tools.bash_tool.get_settings") as mock_settings,
        ):
            mock_settings.return_value.sandbox_code_execution = False

            ctx = _make_ctx("echo hi", tmp_path, use_pty=True, sandbox=True)
            tool.execute_sync(ctx)

        mock_pty.assert_not_called()
        mock_subprocess.assert_called_once()


@pytest.mark.bash
@pytest.mark.unit
class TestPtyImportFallback:
    """Importing ``deile.tools.bash_tool`` must not raise on Windows where
    ``pty`` / ``select`` / ``tty`` aren't shipped.

    Setting ``sys.modules['pty'] = None`` makes the subsequent ``import pty``
    raise ``ImportError`` (PEP 328 sentinel semantics). After reload, the
    module's ``PTY_AVAILABLE`` global must be ``False`` and the rest of the
    module must remain functional.
    """

    def test_module_reloads_with_pty_modules_absent(self) -> None:
        # Snapshot existing references so we don't pollute the test session.
        saved_modules = {
            name: sys.modules.get(name)
            for name in ("pty", "select", "tty", "deile.tools.bash_tool")
        }

        try:
            sys.modules["pty"] = None  # type: ignore[assignment]
            sys.modules["select"] = None  # type: ignore[assignment]
            sys.modules["tty"] = None  # type: ignore[assignment]
            # Force fresh import so the top-level try/except runs.
            sys.modules.pop("deile.tools.bash_tool", None)

            module = importlib.import_module("deile.tools.bash_tool")
            assert module.PTY_AVAILABLE is False
            # Module still defines its public class.
            assert hasattr(module, "BashExecuteTool")
        finally:
            # Restore originals so subsequent tests see a clean import state.
            for name, val in saved_modules.items():
                if val is not None:
                    sys.modules[name] = val
                else:
                    sys.modules.pop(name, None)
            # Reload the module from disk so other tests see the real PTY state.
            importlib.import_module("deile.tools.bash_tool")
