"""Tests for the isolated-venv install functions in deile/cli.py.

Functions under test:
  * _ensure_scripts_dir_on_path(scripts_dir)
  * _user_scripts_dir()
  * _wrapper_target_dir()
  * _create_venv_with_deile(venv_dir, repo_root, mode_label)  [async]
  * _link_global_command(target_dir, source_script, *, force=False)
  * _prompt_install_mode()
  * _run_self_install(mode=None)

No asyncio, no real API calls, no side effects beyond the temp directory.
Venv creation is mocked via unittest.mock — we never call real `venv` or `pip`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.exceptions import DEILEInstallError

# ===================================================================
# _user_scripts_dir
# ===================================================================

@pytest.mark.unit
class TestUserScriptsDir:
    """_user_scripts_dir() — delegates to sysconfig.

    Since sysconfig.get_path depends on the real Python installation,
    we only verify it returns a Path and that the result is non-empty.
    """

    def test_returns_path(self):
        """_user_scripts_dir() always returns a Path."""
        from deile.cli import _user_scripts_dir

        result = _user_scripts_dir()
        assert isinstance(result, Path)
        assert result.name == "bin" or result.name.endswith("Scripts")


# ===================================================================
# _wrapper_target_dir
# ===================================================================

@pytest.mark.unit
class TestWrapperTargetDir:
    """_wrapper_target_dir() — platform-dependent."""

    @patch("deile.cli.os.name", "posix")
    def test_posix_returns_dotlocal_bin(self):
        """On POSIX, returns ~/.local/bin."""
        from deile.cli import _wrapper_target_dir

        result = _wrapper_target_dir()
        assert result == Path.home() / ".local" / "bin"

    @patch("deile.cli.os.name", "nt")
    @patch("deile.cli._user_scripts_dir")
    def test_windows_returns_user_scripts_dir(self, mock_user_scripts):
        """On Windows, delegates to _user_scripts_dir()."""
        from deile.cli import _wrapper_target_dir

        mock_user_scripts.return_value = MagicMock()
        result = _wrapper_target_dir()
        assert result == mock_user_scripts.return_value


# ===================================================================
# _ensure_scripts_dir_on_path
# ===================================================================

@pytest.mark.unit
class TestEnsureScriptsDirOnPath:
    """_ensure_scripts_dir_on_path() — shell detection and rc file editing.

    Returns tuple[bool, Optional[Path], str].
    Security: uses atomic write (tempfile + os.replace), line-by-line check.
    """

    SCRIPTS_DIR = Path("/home/user/.local/bin")
    EXPORT_LINE = f'export PATH="{SCRIPTS_DIR}:$PATH"'

    # -- Windows --

    @patch("deile.cli.os.name", "nt")
    def test_windows_returns_hint(self):
        """Windows -> (False, None, hint)."""
        from deile.cli import _ensure_scripts_dir_on_path

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path is None
        assert "PowerShell" in hint
        assert "System Properties" in hint

    # -- Unknown shell --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="")
    def test_unknown_shell_returns_hint(self, mock_getenv):
        """Unknown shell -> (False, None, hint)."""
        from deile.cli import _ensure_scripts_dir_on_path

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path is None
        assert "Add to your shell rc" in hint

    # -- Zsh: already configured (line-by-line check) --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("deile.cli.Path.read_text")
    def test_zsh_already_configured(self, mock_read, mock_exists, mock_resolve,
                                     mock_home, mock_environ):
        """Zsh with scripts_dir already in .zshrc -> (False, rc, '')."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        mock_read.return_value = 'export PATH="/home/user/.local/bin:$PATH"\n'

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path == Path("/home/user/.zshrc")
        assert hint == ""

    # -- Zsh: commented-out line is NOT treated as configured --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("deile.cli.Path.read_text")
    @patch("deile.cli.tempfile.mkstemp")
    @patch("deile.cli.os.write")
    @patch("deile.cli.os.close")
    @patch("deile.cli.os.replace")
    @patch("deile.cli.Path.mkdir")
    def test_zsh_commented_line_is_not_configured(
        self, mock_mkdir, mock_replace, mock_close, mock_write,
        mock_mkstemp, mock_read, mock_exists, mock_resolve, mock_home,
        mock_environ,
    ):
        """Commented-out PATH line is not treated as configured."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        # Scripts dir appears but commented out — should NOT match
        mock_read.return_value = '# export PATH="/home/user/.local/bin:$PATH"\n'
        mock_mkstemp.return_value = (999, "/home/user/.deile_rc_abc123.tmp")

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.zshrc")
        assert hint == ""

    # -- Bash (Linux): edits .bashrc (atomic write) --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.sys.platform", "linux")
    @patch("deile.cli.os.environ.get", return_value="/bin/bash")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.resolve", return_value=Path("/home/user/.bashrc"))
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.read_text")
    @patch("deile.cli.tempfile.mkstemp")
    @patch("deile.cli.os.write")
    @patch("deile.cli.os.close")
    @patch("deile.cli.os.replace")
    @patch("deile.cli.Path.mkdir")
    def test_bash_linux_edits_bashrc(
        self, mock_mkdir, mock_replace, mock_close, mock_write,
        mock_mkstemp, mock_read, mock_exists, mock_resolve, mock_home,
        mock_environ,
    ):
        """Bash on Linux -> edits .bashrc with export line (atomic write)."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        mock_read.return_value = ""
        mock_mkstemp.return_value = (999, "/home/user/.deile_rc_abc123.tmp")

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.bashrc")
        assert hint == ""

        # Verify atomic write: content written to tempfile, then os.replace
        written_bytes = mock_write.call_args[0][1]
        written_text = written_bytes.decode("utf-8")
        assert "# Added by `deile --install`" in written_text
        assert self.EXPORT_LINE in written_text
        mock_replace.assert_called_once()

    # -- Bash (macOS): edits .bash_profile --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.sys.platform", "darwin")
    @patch("deile.cli.os.environ.get", return_value="/bin/bash")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.resolve", return_value=Path("/home/user/.bash_profile"))
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.read_text")
    @patch("deile.cli.tempfile.mkstemp")
    @patch("deile.cli.os.write")
    @patch("deile.cli.os.close")
    @patch("deile.cli.os.replace")
    @patch("deile.cli.Path.mkdir")
    def test_bash_macos_edits_bash_profile(
        self, mock_mkdir, mock_replace, mock_close, mock_write,
        mock_mkstemp, mock_read, mock_exists, mock_resolve, mock_home,
        mock_environ,
    ):
        """Bash on macOS -> edits .bash_profile with export line."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        mock_read.return_value = ""
        mock_mkstemp.return_value = (999, "/home/user/.deile_rc_abc123.tmp")

        modified, rc_path, _hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.bash_profile")

    # -- Fish: edits config.fish --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/usr/bin/fish")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.resolve", return_value=Path("/home/user/.config/fish/config.fish"))
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.read_text")
    @patch("deile.cli.tempfile.mkstemp")
    @patch("deile.cli.os.write")
    @patch("deile.cli.os.close")
    @patch("deile.cli.os.replace")
    @patch("deile.cli.Path.mkdir")
    def test_fish_edits_config_fish(
        self, mock_mkdir, mock_replace, mock_close, mock_write,
        mock_mkstemp, mock_read, mock_exists, mock_resolve, mock_home,
        mock_environ,
    ):
        """Fish shell -> edits config.fish with set -gx PATH."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        mock_read.return_value = ""
        mock_mkstemp.return_value = (999, "/home/user/.deile_rc_abc123.tmp")

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.config/fish/config.fish")
        written_text = mock_write.call_args[0][1].decode("utf-8")
        assert "set -gx PATH" in written_text

    # -- Read error --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("deile.cli.Path.read_text", side_effect=OSError("Permission denied"))
    def test_read_error_returns_hint(self, mock_read, mock_exists, mock_resolve,
                                      mock_home, mock_environ):
        """OSError on read -> (False, rc, hint)."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path == Path("/home/user/.zshrc")
        assert "Could not read" in hint

    # -- Write error (raises DEILEInstallError) --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.read_text")
    @patch("deile.cli.Path.mkdir")
    @patch("deile.cli.tempfile.mkstemp", side_effect=OSError("Read-only filesystem"))
    def test_write_error_raises_deile_install_error(
        self, mock_mkstemp, mock_mkdir, mock_read, mock_exists,
        mock_resolve, mock_home, mock_environ,
    ):
        """OSError on tempfile creation -> DEILEInstallError."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        mock_read.return_value = ""

        with pytest.raises(DEILEInstallError, match="Could not write"):
            _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)

    # -- Symlink traversal blocked --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli.Path.home")
    def test_symlink_outside_home_blocked(self, mock_home, mock_environ):
        """If rc resolves outside $HOME, raises DEILEInstallError."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user")

        # resolve() is called twice inside the function:
        #   1. home = Path.home().resolve()   → /home/user (canonical home)
        #   2. rc   = (home / ".zshrc").resolve() → /etc/.zshrc (symlink outside home)
        # side_effect gives a different value per call so the security check triggers.
        with patch("deile.cli.Path.resolve", side_effect=[Path("/home/user"), Path("/etc/.zshrc")]):
            with pytest.raises(DEILEInstallError, match="outside of home"):
                _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)


# ===================================================================
# _prompt_install_mode
# ===================================================================

@pytest.mark.unit
class TestPromptInstallMode:
    """_prompt_install_mode() — interactive choice."""

    @patch("builtins.input", return_value="")
    def test_default_is_global(self, mock_input):
        """Empty choice (ENTER) -> 'global'."""
        from deile.cli import _prompt_install_mode

        assert _prompt_install_mode() == "global"

    @patch("builtins.input", return_value="g")
    def test_g_returns_global(self, mock_input):
        """'g' -> 'global'."""
        from deile.cli import _prompt_install_mode

        assert _prompt_install_mode() == "global"

    @patch("builtins.input", return_value="global")
    def test_global_returns_global(self, mock_input):
        """'global' -> 'global'."""
        from deile.cli import _prompt_install_mode

        assert _prompt_install_mode() == "global"

    @patch("builtins.input", return_value="l")
    def test_l_returns_local(self, mock_input):
        """'l' -> 'local'."""
        from deile.cli import _prompt_install_mode

        assert _prompt_install_mode() == "local"

    @patch("builtins.input", return_value="local")
    def test_local_returns_local(self, mock_input):
        """'local' -> 'local'."""
        from deile.cli import _prompt_install_mode

        assert _prompt_install_mode() == "local"

    @patch("builtins.input", return_value="q")
    def test_q_returns_none(self, mock_input):
        """'q' -> None."""
        from deile.cli import _prompt_install_mode

        assert _prompt_install_mode() is None

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_returns_none(self, mock_input):
        """KeyboardInterrupt -> None."""
        from deile.cli import _prompt_install_mode

        assert _prompt_install_mode() is None

    @patch("builtins.input", side_effect=EOFError)
    def test_eof_error_returns_none(self, mock_input):
        """EOFError -> None."""
        from deile.cli import _prompt_install_mode

        assert _prompt_install_mode() is None


# ===================================================================
# _link_global_command
# ===================================================================

@pytest.mark.unit
class TestLinkGlobalCommand:
    """_link_global_command() — symlink (POSIX) or .cmd shim (Windows)."""

    SOURCE = Path("/home/user/.deile/venv/bin/deile")
    TARGET_DIR = Path("/home/user/.local/bin")

    # -- POSIX: symlink creation --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.Path.mkdir")
    @patch("deile.cli.Path.is_symlink", return_value=False)
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.symlink_to")
    def test_posix_creates_symlink(self, mock_symlink, mock_exists,
                                    mock_is_symlink, mock_mkdir):
        """POSIX: creates symlink at target_dir/deile -> source_script."""
        from deile.cli import _link_global_command

        result = _link_global_command(self.TARGET_DIR, self.SOURCE)

        expected_target = self.TARGET_DIR / "deile"
        assert result == expected_target
        mock_symlink.assert_called_once_with(self.SOURCE)

    # -- POSIX: force overwrite --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.Path.mkdir")
    @patch("deile.cli.Path.is_symlink", return_value=True)
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("deile.cli.Path.unlink")
    @patch("deile.cli.Path.symlink_to")
    @patch("builtins.input", return_value="y")
    def test_posix_force_overwrite(self, mock_input, mock_symlink, mock_unlink,
                                    mock_exists, mock_is_symlink, mock_mkdir):
        """POSIX: existing symlink removed and recreated when user agrees."""
        from deile.cli import _link_global_command

        link_target = _link_global_command(self.TARGET_DIR, self.SOURCE)
        assert link_target == self.TARGET_DIR / "deile"
        mock_unlink.assert_called_once()
        mock_symlink.assert_called_once_with(self.SOURCE)

    # -- POSIX: refuses overwrite (DEILEInstallError) --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.Path.mkdir")
    @patch("deile.cli.Path.is_symlink", return_value=True)
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("builtins.input", return_value="n")
    def test_posix_refuses_overwrite(self, mock_input, mock_exists,
                                      mock_is_symlink, mock_mkdir):
        """POSIX: user says no -> DEILEInstallError."""
        from deile.cli import _link_global_command

        with pytest.raises(DEILEInstallError, match="refusing to overwrite"):
            _link_global_command(self.TARGET_DIR, self.SOURCE)

    # -- Windows: .cmd shim creation --

    @patch("deile.cli.os.name", "nt")
    @patch("deile.cli.Path.mkdir")
    @patch("deile.cli.Path.is_symlink", return_value=False)
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.write_text")
    def test_windows_creates_cmd_shim(self, mock_write, mock_exists,
                                       mock_is_symlink, mock_mkdir):
        """Windows: creates deile.cmd with @echo off."""
        from deile.cli import _link_global_command

        result = _link_global_command(self.TARGET_DIR, self.SOURCE)

        expected_target = self.TARGET_DIR / "deile.cmd"
        assert result == expected_target
        written = mock_write.call_args[0][0]
        assert "@echo off" in written
        assert str(self.SOURCE) in written


# ===================================================================
# _create_venv_with_deile  (mocked venv + asyncio subprocess)
# ===================================================================

@pytest.mark.unit
class TestCreateVenvWithDeile:
    """_create_venv_with_deile() [async] — mocks venv creation and pip subprocesses."""

    VENV_DIR = Path("/tmp/test-venv")
    REPO_ROOT = Path("/tmp/test-repo")

    async def _run_async(self, coro):
        """Helper to run async function in sync test."""
        return await coro

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.Path.resolve")
    @patch("deile.cli.Path.exists")
    @patch("deile.cli.Path.mkdir")
    @patch("deile.cli.asyncio.create_subprocess_exec")
    def test_creates_venv_and_installs(self, mock_subproc, mock_mkdir,
                                        mock_exists, mock_resolve):
        """Full pipeline: create venv -> upgrade pip -> install reqs -> editable."""
        from deile.cli import _create_venv_with_deile

        # Path.resolve is called 3 times inside the function:
        #   1. venv_dir.resolve()        → VENV_DIR
        #   2. repo_root.resolve()       → REPO_ROOT
        #   3. Path.home().resolve()     → /tmp (satisfies venv_dir startswith check)
        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, Path("/tmp")]

        # _create_venv_with_deile calls .exists() 3 times:
        #   1. venv_py.exists()      -> False (trigger creation)
        #   2. requirements.exists() -> True  (pip install -r)
        #   3. deile_script.exists() -> True  (verify script was created)
        mock_exists.side_effect = [False, True, True]

        # asyncio.create_subprocess_exec is a coroutine — use AsyncMock so
        # `await asyncio.create_subprocess_exec(...)` returns mock_proc correctly.
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subproc.return_value = mock_proc

        deile_script = self.VENV_DIR / "bin/deile"

        with patch("deile.cli._venv.EnvBuilder") as mock_env_builder:
            mock_env_builder.return_value = MagicMock()
            with patch("deile.cli.asyncio.to_thread") as mock_to_thread:
                result = asyncio.run(_create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test"))

        mock_to_thread.assert_called_once()
        assert mock_subproc.call_count >= 3

        call_args_0 = mock_subproc.call_args_list[0][0]
        assert "--upgrade" in call_args_0
        assert "pip" in call_args_0

        call_args_1 = mock_subproc.call_args_list[1][0]
        assert "-r" in call_args_1

        call_args_2 = mock_subproc.call_args_list[2][0]
        assert "--no-deps" in call_args_2
        assert "-e" in call_args_2

        assert result == deile_script

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.Path.resolve")
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("deile.cli.asyncio.create_subprocess_exec")
    def test_reuses_existing_venv(self, mock_subproc, mock_exists, mock_resolve):
        """If venv python already exists, skips creation."""
        from deile.cli import _create_venv_with_deile

        # 3 resolve() calls: venv_dir, repo_root, Path.home()
        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, Path("/tmp")]

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subproc.return_value = mock_proc

        with patch("deile.cli._venv.EnvBuilder"):
            with patch("deile.cli.asyncio.to_thread") as mock_to_thread:
                asyncio.run(_create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test"))

        mock_to_thread.assert_not_called()
        pip_calls = [c[0] for c in mock_subproc.call_args_list]
        assert len(pip_calls) >= 2  # still upgrades pip and installs


# ===================================================================
# _run_self_install  (integration-style with all helpers mocked)
# ===================================================================

@pytest.mark.unit
class TestRunSelfInstall:
    """_run_self_install() — end-to-end with all helpers mocked."""

    @patch("deile.cli._prompt_install_mode", return_value="global")
    @patch("deile.cli._create_venv_with_deile")
    @patch("deile.cli._wrapper_target_dir")
    @patch("deile.cli._link_global_command")
    @patch("deile.cli._ensure_scripts_dir_on_path", return_value=(True, Path("/home/user/.zshrc"), ""))
    @patch("deile.cli.subprocess.run")
    @patch("builtins.print")
    def test_global_mode_full_flow(
        self, mock_print, mock_subprocess, mock_ensure_path,
        mock_link, mock_wrapper_dir, mock_create_venv, mock_prompt,
    ):
        """Global mode w/ mode=None (interactive prompt) -> all helpers called."""
        from deile.cli import _run_self_install

        mock_create_venv.return_value = Path("/home/user/.deile/venv/bin/deile")
        mock_wrapper_dir.return_value = Path("/home/user/.local/bin")
        mock_link.return_value = Path("/home/user/.local/bin/deile")
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        result = _run_self_install(mode=None)

        assert result == 0
        mock_prompt.assert_called_once()
        mock_create_venv.assert_called_once()
        mock_link.assert_called_once()
        mock_ensure_path.assert_called_once()

    @patch("deile.cli._prompt_install_mode", return_value=None)
    @patch("builtins.print")
    def test_cancelled_returns_1(self, mock_print, mock_prompt):
        """User cancels prompt -> returns 1."""
        from deile.cli import _run_self_install

        assert _run_self_install(mode=None) == 1

    @patch("deile.cli._create_venv_with_deile",
           side_effect=DEILEInstallError("venv creation failed", step="create_venv"))
    @patch("builtins.print")
    def test_venv_error_returns_1(self, mock_print, mock_create_venv):
        """DEILEInstallError from _create_venv_with_deile -> returns 1."""
        from deile.cli import _run_self_install

        assert _run_self_install(mode="global") == 1

    @patch("deile.cli._create_venv_with_deile")
    @patch("deile.cli._wrapper_target_dir")
    @patch("deile.cli._link_global_command",
           side_effect=DEILEInstallError("refusing to overwrite", step="link_command"))
    @patch("builtins.print")
    def test_link_error_returns_1(
        self, mock_print, mock_link, mock_wrapper_dir, mock_create_venv,
    ):
        """DEILEInstallError from _link_global_command -> returns 1."""
        from deile.cli import _run_self_install

        mock_create_venv.return_value = Path("/tmp/venv/bin/deile")
        mock_wrapper_dir.return_value = Path("/home/user/.local/bin")

        assert _run_self_install(mode="global") == 1

    @patch("deile.cli._create_venv_with_deile")
    @patch("deile.cli._wrapper_target_dir")
    @patch("deile.cli._link_global_command")
    @patch("deile.cli._ensure_scripts_dir_on_path",
           return_value=(True, Path("/home/user/.zshrc"), ""))
    @patch("deile.cli.subprocess.run")
    @patch("builtins.print")
    def test_local_mode(
        self, mock_print, mock_subprocess, mock_ensure_path,
        mock_link, mock_wrapper_dir, mock_create_venv,
    ):
        """Local mode with mode='local' -> .venv in repo root."""
        from deile.cli import _run_self_install

        mock_create_venv.return_value = Path("/tmp/repo/.venv/bin/deile")
        mock_wrapper_dir.return_value = Path("/home/user/.local/bin")
        mock_link.return_value = Path("/home/user/.local/bin/deile")
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        result = _run_self_install(mode="local")

        assert result == 0
        # Verify venv was created in repo_root/.venv
        venv_dir_arg = mock_create_venv.call_args[0][0]
        assert venv_dir_arg.name == ".venv"

    def test_unknown_mode_returns_2(self):
        """Unknown mode string -> returns 2."""
        from deile.cli import _run_self_install

        assert _run_self_install(mode="invalid") == 2
