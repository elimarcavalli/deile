"""Tests for the isolated-venv install functions in deile/cli_install.py.

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
        from deile.cli_install import _user_scripts_dir

        result = _user_scripts_dir()
        assert isinstance(result, Path)
        assert result.name == "bin" or result.name.endswith("Scripts")


# ===================================================================
# _wrapper_target_dir
# ===================================================================


@pytest.mark.unit
class TestWrapperTargetDir:
    """_wrapper_target_dir() — platform-dependent."""

    @patch("deile.cli_install.os.name", "posix")
    def test_posix_returns_dotlocal_bin(self):
        """On POSIX, returns ~/.local/bin."""
        from deile.cli_install import _wrapper_target_dir

        result = _wrapper_target_dir()
        assert result == Path.home() / ".local" / "bin"

    @patch("deile.cli_install.os.name", "nt")
    @patch("deile.cli_install._user_scripts_dir")
    def test_windows_returns_user_scripts_dir(self, mock_user_scripts):
        """On Windows, delegates to _user_scripts_dir()."""
        from deile.cli_install import _wrapper_target_dir

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

    @patch("deile.cli_install.os.name", "nt")
    def test_windows_returns_hint(self):
        """Windows -> (False, None, hint)."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path is None
        assert "PowerShell" in hint
        assert "System Properties" in hint

    # -- Unknown shell --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.os.environ.get", return_value="")
    def test_unknown_shell_returns_hint(self, mock_getenv):
        """Unknown shell -> (False, None, hint)."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path is None
        assert "Add to your shell rc" in hint

    # -- Zsh: already configured (line-by-line check) --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("deile.cli_install.Path.read_text")
    def test_zsh_already_configured(
        self, mock_read, mock_exists, mock_resolve, mock_home, mock_environ
    ):
        """Zsh with scripts_dir already in .zshrc -> (False, rc, '')."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        mock_read.return_value = 'export PATH="/home/user/.local/bin:$PATH"\n'

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path == Path("/home/user/.zshrc")
        assert hint == ""

    # -- Zsh: commented-out line is NOT treated as configured --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("deile.cli_install.Path.read_text")
    @patch("deile.cli_install.tempfile.mkstemp")
    @patch("deile.cli_install.os.write")
    @patch("deile.cli_install.os.close")
    @patch("deile.cli_install.os.replace")
    @patch("deile.cli_install.Path.mkdir")
    def test_zsh_commented_line_is_not_configured(
        self,
        mock_mkdir,
        mock_replace,
        mock_close,
        mock_write,
        mock_mkstemp,
        mock_read,
        mock_exists,
        mock_resolve,
        mock_home,
        mock_environ,
    ):
        """Commented-out PATH line is not treated as configured."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        # Scripts dir appears but commented out — should NOT match
        mock_read.return_value = '# export PATH="/home/user/.local/bin:$PATH"\n'
        mock_mkstemp.return_value = (999, "/home/user/.deile_rc_abc123.tmp")

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.zshrc")
        assert hint == ""

    # -- Bash (Linux): edits .bashrc (atomic write) --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.sys.platform", "linux")
    @patch("deile.cli_install.os.environ.get", return_value="/bin/bash")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install.Path.resolve", return_value=Path("/home/user/.bashrc"))
    @patch("deile.cli_install.Path.exists", return_value=False)
    @patch("deile.cli_install.Path.read_text")
    @patch("deile.cli_install.tempfile.mkstemp")
    @patch("deile.cli_install.os.write")
    @patch("deile.cli_install.os.close")
    @patch("deile.cli_install.os.replace")
    @patch("deile.cli_install.Path.mkdir")
    def test_bash_linux_edits_bashrc(
        self,
        mock_mkdir,
        mock_replace,
        mock_close,
        mock_write,
        mock_mkstemp,
        mock_read,
        mock_exists,
        mock_resolve,
        mock_home,
        mock_environ,
    ):
        """Bash on Linux -> edits .bashrc with export line (atomic write)."""
        from deile.cli_install import _ensure_scripts_dir_on_path

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

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.sys.platform", "darwin")
    @patch("deile.cli_install.os.environ.get", return_value="/bin/bash")
    @patch("deile.cli_install.Path.home")
    @patch(
        "deile.cli_install.Path.resolve", return_value=Path("/home/user/.bash_profile")
    )
    @patch("deile.cli_install.Path.exists", return_value=False)
    @patch("deile.cli_install.Path.read_text")
    @patch("deile.cli_install.tempfile.mkstemp")
    @patch("deile.cli_install.os.write")
    @patch("deile.cli_install.os.close")
    @patch("deile.cli_install.os.replace")
    @patch("deile.cli_install.Path.mkdir")
    def test_bash_macos_edits_bash_profile(
        self,
        mock_mkdir,
        mock_replace,
        mock_close,
        mock_write,
        mock_mkstemp,
        mock_read,
        mock_exists,
        mock_resolve,
        mock_home,
        mock_environ,
    ):
        """Bash on macOS -> edits .bash_profile with export line."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        mock_read.return_value = ""
        mock_mkstemp.return_value = (999, "/home/user/.deile_rc_abc123.tmp")

        modified, rc_path, _hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.bash_profile")

    # -- Fish: edits config.fish --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.os.environ.get", return_value="/usr/bin/fish")
    @patch("deile.cli_install.Path.home")
    @patch(
        "deile.cli_install.Path.resolve",
        return_value=Path("/home/user/.config/fish/config.fish"),
    )
    @patch("deile.cli_install.Path.exists", return_value=False)
    @patch("deile.cli_install.Path.read_text")
    @patch("deile.cli_install.tempfile.mkstemp")
    @patch("deile.cli_install.os.write")
    @patch("deile.cli_install.os.close")
    @patch("deile.cli_install.os.replace")
    @patch("deile.cli_install.Path.mkdir")
    def test_fish_edits_config_fish(
        self,
        mock_mkdir,
        mock_replace,
        mock_close,
        mock_write,
        mock_mkstemp,
        mock_read,
        mock_exists,
        mock_resolve,
        mock_home,
        mock_environ,
    ):
        """Fish shell -> edits config.fish with set -gx PATH."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        mock_read.return_value = ""
        mock_mkstemp.return_value = (999, "/home/user/.deile_rc_abc123.tmp")

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.config/fish/config.fish")
        written_text = mock_write.call_args[0][1].decode("utf-8")
        assert "set -gx PATH" in written_text

    # -- Read error --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("deile.cli_install.Path.read_text", side_effect=OSError("Permission denied"))
    def test_read_error_returns_hint(
        self, mock_read, mock_exists, mock_resolve, mock_home, mock_environ
    ):
        """OSError on read -> (False, rc, hint)."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path == Path("/home/user/.zshrc")
        assert "Could not read" in hint

    # -- Write error (raises DEILEInstallError) --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli_install.Path.exists", return_value=False)
    @patch("deile.cli_install.Path.read_text")
    @patch("deile.cli_install.Path.mkdir")
    @patch(
        "deile.cli_install.tempfile.mkstemp",
        side_effect=OSError("Read-only filesystem"),
    )
    def test_write_error_raises_deile_install_error(
        self,
        mock_mkstemp,
        mock_mkdir,
        mock_read,
        mock_exists,
        mock_resolve,
        mock_home,
        mock_environ,
    ):
        """OSError on tempfile creation -> DEILEInstallError."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        mock_read.return_value = ""

        with pytest.raises(DEILEInstallError, match="Could not write"):
            _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)

    # -- Symlink traversal blocked --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli_install.Path.home")
    def test_symlink_outside_home_blocked(self, mock_home, mock_environ):
        """If rc resolves outside $HOME, raises DEILEInstallError."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user")

        # resolve() is called twice inside the function:
        #   1. home = Path.home().resolve()   → /home/user (canonical home)
        #   2. rc   = (home / ".zshrc").resolve() → /etc/.zshrc (symlink outside home)
        # side_effect gives a different value per call so the security check triggers.
        with patch(
            "deile.cli_install.Path.resolve",
            side_effect=[Path("/home/user"), Path("/etc/.zshrc")],
        ):
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
        from deile.cli_install import _prompt_install_mode

        assert _prompt_install_mode() == "global"

    @patch("builtins.input", return_value="g")
    def test_g_returns_global(self, mock_input):
        """'g' -> 'global'."""
        from deile.cli_install import _prompt_install_mode

        assert _prompt_install_mode() == "global"

    @patch("builtins.input", return_value="global")
    def test_global_returns_global(self, mock_input):
        """'global' -> 'global'."""
        from deile.cli_install import _prompt_install_mode

        assert _prompt_install_mode() == "global"

    @patch("builtins.input", return_value="l")
    def test_l_returns_local(self, mock_input):
        """'l' -> 'local'."""
        from deile.cli_install import _prompt_install_mode

        assert _prompt_install_mode() == "local"

    @patch("builtins.input", return_value="local")
    def test_local_returns_local(self, mock_input):
        """'local' -> 'local'."""
        from deile.cli_install import _prompt_install_mode

        assert _prompt_install_mode() == "local"

    @patch("builtins.input", return_value="q")
    def test_q_returns_none(self, mock_input):
        """'q' -> None."""
        from deile.cli_install import _prompt_install_mode

        assert _prompt_install_mode() is None

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_returns_none(self, mock_input):
        """KeyboardInterrupt -> None."""
        from deile.cli_install import _prompt_install_mode

        assert _prompt_install_mode() is None

    @patch("builtins.input", side_effect=EOFError)
    def test_eof_error_returns_none(self, mock_input):
        """EOFError -> None."""
        from deile.cli_install import _prompt_install_mode

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

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.Path.is_symlink", return_value=False)
    @patch("deile.cli_install.Path.exists", return_value=False)
    @patch("deile.cli_install.Path.symlink_to")
    def test_posix_creates_symlink(
        self, mock_symlink, mock_exists, mock_is_symlink, mock_mkdir
    ):
        """POSIX: creates symlink at target_dir/deile -> source_script."""
        from deile.cli_install import _link_global_command

        result = _link_global_command(self.TARGET_DIR, self.SOURCE)

        expected_target = self.TARGET_DIR / "deile"
        assert result == expected_target
        mock_symlink.assert_called_once_with(self.SOURCE)

    # -- POSIX: force overwrite --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.Path.is_symlink", return_value=True)
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("deile.cli_install.Path.unlink")
    @patch("deile.cli_install.Path.symlink_to")
    @patch("builtins.input", return_value="y")
    def test_posix_force_overwrite(
        self,
        mock_input,
        mock_symlink,
        mock_unlink,
        mock_exists,
        mock_is_symlink,
        mock_mkdir,
    ):
        """POSIX: existing symlink removed and recreated when user agrees."""
        from deile.cli_install import _link_global_command

        link_target = _link_global_command(self.TARGET_DIR, self.SOURCE)
        assert link_target == self.TARGET_DIR / "deile"
        mock_unlink.assert_called_once()
        mock_symlink.assert_called_once_with(self.SOURCE)

    # -- POSIX: refuses overwrite (DEILEInstallError) --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.Path.is_symlink", return_value=True)
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("builtins.input", return_value="n")
    def test_posix_refuses_overwrite(
        self, mock_input, mock_exists, mock_is_symlink, mock_mkdir
    ):
        """POSIX: user says no -> DEILEInstallError."""
        from deile.cli_install import _link_global_command

        with pytest.raises(DEILEInstallError, match="refusing to overwrite"):
            _link_global_command(self.TARGET_DIR, self.SOURCE)

    # -- Windows: .cmd shim creation --

    @patch("deile.cli_install.os.name", "nt")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.Path.is_symlink", return_value=False)
    @patch("deile.cli_install.Path.exists", return_value=False)
    @patch("deile.cli_install.Path.write_text")
    def test_windows_creates_cmd_shim(
        self, mock_write, mock_exists, mock_is_symlink, mock_mkdir
    ):
        """Windows: creates deile.cmd with @echo off."""
        from deile.cli_install import _link_global_command

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

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.resolve")
    @patch("deile.cli_install.Path.exists")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.asyncio.create_subprocess_exec")
    def test_creates_venv_and_installs(
        self, mock_subproc, mock_mkdir, mock_exists, mock_resolve
    ):
        """Full pipeline: create venv -> upgrade pip -> install reqs -> editable."""
        from deile.cli_install import _create_venv_with_deile

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

        with patch("deile.cli_install._venv.EnvBuilder") as mock_env_builder:
            mock_env_builder.return_value = MagicMock()
            with patch("deile.cli_install.asyncio.to_thread") as mock_to_thread:
                result = asyncio.run(
                    _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")
                )

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

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.resolve")
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("deile.cli_install.asyncio.create_subprocess_exec")
    def test_reuses_existing_venv(self, mock_subproc, mock_exists, mock_resolve):
        """If venv python already exists, skips creation."""
        from deile.cli_install import _create_venv_with_deile

        # 3 resolve() calls: venv_dir, repo_root, Path.home()
        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, Path("/tmp")]

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subproc.return_value = mock_proc

        with patch("deile.cli_install._venv.EnvBuilder"):
            with patch("deile.cli_install.asyncio.to_thread") as mock_to_thread:
                asyncio.run(
                    _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")
                )

        mock_to_thread.assert_not_called()
        pip_calls = [c[0] for c in mock_subproc.call_args_list]
        assert len(pip_calls) >= 2  # still upgrades pip and installs


# ===================================================================
# _run_self_install  (integration-style with all helpers mocked)
# ===================================================================


@pytest.mark.unit
class TestRunSelfInstall:
    """_run_self_install() — end-to-end with all helpers mocked."""

    @patch("deile.cli_install._prompt_install_mode", return_value="global")
    @patch("deile.cli_install._create_venv_with_deile")
    @patch("deile.cli_install._wrapper_target_dir")
    @patch("deile.cli_install._link_global_command")
    @patch(
        "deile.cli_install._ensure_scripts_dir_on_path",
        return_value=(True, Path("/home/user/.zshrc"), ""),
    )
    @patch("deile.cli_install.subprocess.run")
    @patch("builtins.print")
    def test_global_mode_full_flow(
        self,
        mock_print,
        mock_subprocess,
        mock_ensure_path,
        mock_link,
        mock_wrapper_dir,
        mock_create_venv,
        mock_prompt,
    ):
        """Global mode w/ mode=None (interactive prompt) -> all helpers called."""
        from deile.cli_install import _run_self_install

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

    @patch("deile.cli_install._prompt_install_mode", return_value=None)
    @patch("builtins.print")
    def test_cancelled_returns_1(self, mock_print, mock_prompt):
        """User cancels prompt -> returns 1."""
        from deile.cli_install import _run_self_install

        assert _run_self_install(mode=None) == 1

    @patch(
        "deile.cli_install._create_venv_with_deile",
        side_effect=DEILEInstallError("venv creation failed", step="create_venv"),
    )
    @patch("builtins.print")
    def test_venv_error_returns_1(self, mock_print, mock_create_venv):
        """DEILEInstallError from _create_venv_with_deile -> returns 1."""
        from deile.cli_install import _run_self_install

        assert _run_self_install(mode="global") == 1

    @patch("deile.cli_install._create_venv_with_deile")
    @patch("deile.cli_install._wrapper_target_dir")
    @patch(
        "deile.cli_install._link_global_command",
        side_effect=DEILEInstallError("refusing to overwrite", step="link_command"),
    )
    @patch("builtins.print")
    def test_link_error_returns_1(
        self,
        mock_print,
        mock_link,
        mock_wrapper_dir,
        mock_create_venv,
    ):
        """DEILEInstallError from _link_global_command -> returns 1."""
        from deile.cli_install import _run_self_install

        mock_create_venv.return_value = Path("/tmp/venv/bin/deile")
        mock_wrapper_dir.return_value = Path("/home/user/.local/bin")

        assert _run_self_install(mode="global") == 1

    @patch("deile.cli_install._create_venv_with_deile")
    @patch("deile.cli_install._wrapper_target_dir")
    @patch("deile.cli_install._link_global_command")
    @patch(
        "deile.cli_install._ensure_scripts_dir_on_path",
        return_value=(True, Path("/home/user/.zshrc"), ""),
    )
    @patch("deile.cli_install.subprocess.run")
    @patch("builtins.print")
    def test_local_mode(
        self,
        mock_print,
        mock_subprocess,
        mock_ensure_path,
        mock_link,
        mock_wrapper_dir,
        mock_create_venv,
    ):
        """Local mode with mode='local' -> .venv in repo root."""
        from deile.cli_install import _run_self_install

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
        from deile.cli_install import _run_self_install

        assert _run_self_install(mode="invalid") == 2


# ===================================================================
# Additional _link_global_command edge cases (MINOR 13)
# ===================================================================


@pytest.mark.unit
class TestLinkGlobalCommandEdgeCases:
    """Edge cases for _link_global_command that were missing coverage."""

    SOURCE = Path("/home/user/.deile/venv/bin/deile")
    TARGET_DIR = Path("/home/user/.local/bin")

    # -- POSIX: symlink_to raises OSError -> DEILEInstallError --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.Path.is_symlink", return_value=False)
    @patch("deile.cli_install.Path.exists", return_value=False)
    @patch(
        "deile.cli_install.Path.symlink_to", side_effect=OSError("permission denied")
    )
    def test_posix_symlink_oserror_raises(
        self, mock_symlink, mock_exists, mock_is_symlink, mock_mkdir
    ):
        """POSIX: OSError from symlink_to -> DEILEInstallError."""
        from deile.cli_install import _link_global_command

        with pytest.raises(DEILEInstallError, match="could not create symlink"):
            _link_global_command(self.TARGET_DIR, self.SOURCE)

    # -- POSIX: unlink raises OSError -> DEILEInstallError --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.Path.is_symlink", return_value=True)
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("deile.cli_install.Path.unlink", side_effect=OSError("read-only filesystem"))
    @patch("builtins.input", return_value="y")
    def test_posix_unlink_oserror_raises(
        self, mock_input, mock_unlink, mock_exists, mock_is_symlink, mock_mkdir
    ):
        """POSIX: OSError from unlink -> DEILEInstallError."""
        from deile.cli_install import _link_global_command

        with pytest.raises(DEILEInstallError, match="could not remove existing shim"):
            _link_global_command(self.TARGET_DIR, self.SOURCE)

    # -- POSIX: force=True skips prompt, replaces silently --

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.Path.is_symlink", return_value=True)
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("deile.cli_install.Path.unlink")
    @patch("deile.cli_install.Path.symlink_to")
    @patch("builtins.input")
    def test_posix_force_true_skips_prompt(
        self,
        mock_input,
        mock_symlink,
        mock_unlink,
        mock_exists,
        mock_is_symlink,
        mock_mkdir,
    ):
        """POSIX: force=True replaces existing shim without prompting."""
        from deile.cli_install import _link_global_command

        result = _link_global_command(self.TARGET_DIR, self.SOURCE, force=True)

        assert result == self.TARGET_DIR / "deile"
        # input() must not be called when force=True
        mock_input.assert_not_called()
        mock_unlink.assert_called_once()
        mock_symlink.assert_called_once_with(self.SOURCE)


# ===================================================================
# Additional _create_venv_with_deile edge cases (MINOR 12 + 16)
# ===================================================================


@pytest.mark.unit
class TestCreateVenvEdgeCases:
    """Missing edge cases for _create_venv_with_deile."""

    VENV_DIR = Path("/tmp/test-venv")
    REPO_ROOT = Path("/tmp/test-repo")

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.resolve")
    @patch("deile.cli_install.Path.exists")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.asyncio.create_subprocess_exec")
    def test_pip_upgrade_failure_raises(
        self, mock_subproc, mock_mkdir, mock_exists, mock_resolve
    ):
        """If pip upgrade fails (rc != 0), DEILEInstallError is raised."""
        from deile.cli_install import _create_venv_with_deile

        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, Path("/tmp")]
        # venv_py does NOT exist (trigger creation), then upgrade pip is called
        mock_exists.side_effect = [False]

        failing_proc = MagicMock()
        failing_proc.returncode = 1
        failing_proc.communicate = AsyncMock(return_value=(b"", b"error output"))
        mock_subproc.return_value = failing_proc

        with patch("deile.cli_install._venv.EnvBuilder"):
            with patch("deile.cli_install.asyncio.to_thread"):
                with pytest.raises(DEILEInstallError, match="pip upgrade_pip failed"):
                    asyncio.run(
                        _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")
                    )

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.resolve")
    @patch("deile.cli_install.Path.exists")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.asyncio.create_subprocess_exec")
    def test_missing_console_script_raises(
        self, mock_subproc, mock_mkdir, mock_exists, mock_resolve
    ):
        """If console script missing after install, DEILEInstallError raised."""
        from deile.cli_install import _create_venv_with_deile

        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, Path("/tmp")]
        # venv_py absent, requirements.txt present, deile_script absent
        mock_exists.side_effect = [False, True, False]

        ok_proc = MagicMock()
        ok_proc.returncode = 0
        ok_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subproc.return_value = ok_proc

        with patch("deile.cli_install._venv.EnvBuilder"):
            with patch("deile.cli_install.asyncio.to_thread"):
                with pytest.raises(
                    DEILEInstallError, match="console script not created"
                ):
                    asyncio.run(
                        _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")
                    )

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.resolve")
    @patch("deile.cli_install.Path.exists")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.asyncio.create_subprocess_exec")
    def test_editable_install_failure_raises(
        self, mock_subproc, mock_mkdir, mock_exists, mock_resolve
    ):
        """If editable install (--no-deps -e) fails, DEILEInstallError raised."""
        from deile.cli_install import _create_venv_with_deile

        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, Path("/tmp")]
        # venv_py absent, requirements.txt present, but deile_script won't matter
        # because pip install -e fails before we get to the exists() check
        mock_exists.side_effect = [False, True]

        # First two pip calls succeed (upgrade, install deps), third fails (editable)
        ok_proc = MagicMock()
        ok_proc.returncode = 0
        ok_proc.communicate = AsyncMock(return_value=(b"", b""))

        fail_proc = MagicMock()
        fail_proc.returncode = 1
        fail_proc.communicate = AsyncMock(return_value=(b"", b"editable install error"))

        mock_subproc.side_effect = [ok_proc, ok_proc, fail_proc]

        with patch("deile.cli_install._venv.EnvBuilder"):
            with patch("deile.cli_install.asyncio.to_thread"):
                with pytest.raises(
                    DEILEInstallError, match="pip install_editable failed"
                ):
                    asyncio.run(
                        _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")
                    )

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.resolve")
    @patch("deile.cli_install.Path.exists")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.asyncio.create_subprocess_exec")
    def test_requirements_absent_skips_dep_install(
        self, mock_subproc, mock_mkdir, mock_exists, mock_resolve
    ):
        """If requirements.txt is absent, dep install is skipped (no assert on warning)."""
        from deile.cli_install import _create_venv_with_deile

        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, Path("/tmp")]
        # venv_py exists (reuse), requirements.txt absent, deile_script exists
        mock_exists.side_effect = [True, False, True]

        ok_proc = MagicMock()
        ok_proc.returncode = 0
        ok_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subproc.return_value = ok_proc

        with patch("deile.cli_install._venv.EnvBuilder"):
            with patch("deile.cli_install.asyncio.to_thread"):
                result = asyncio.run(
                    _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")
                )

        # Only 2 pip calls: upgrade pip + editable (no -r requirements)
        assert mock_subproc.call_count == 2
        pip_args = [c[0] for c in mock_subproc.call_args_list]
        assert not any("-r" in args for args in pip_args)
        assert result == self.VENV_DIR / "bin/deile"


# ===================================================================
# Additional _create_venv_with_deile assertion fix (MINOR 14)
# ===================================================================


@pytest.mark.unit
class TestReuseVenvAssertionStrength:
    """Verify that test_reuses_existing_venv checks .create() not called (MINOR 14)."""

    VENV_DIR = Path("/tmp/test-venv")
    REPO_ROOT = Path("/tmp/test-repo")

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.Path.resolve")
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("deile.cli_install.asyncio.create_subprocess_exec")
    def test_reuses_venv_create_not_called(
        self, mock_subproc, mock_exists, mock_resolve
    ):
        """When venv python already exists, EnvBuilder.create must NOT be called."""
        from deile.cli_install import _create_venv_with_deile

        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, Path("/tmp")]

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subproc.return_value = mock_proc

        with patch("deile.cli_install._venv.EnvBuilder") as mock_env_builder:
            with patch("deile.cli_install.asyncio.to_thread") as mock_to_thread:
                asyncio.run(
                    _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")
                )

        # The critical assertion: .create() must not be called (not just EnvBuilder())
        mock_env_builder.return_value.create.assert_not_called()
        mock_to_thread.assert_not_called()


# ===================================================================
# Additional _ensure_scripts_dir_on_path: rc file content preservation (MINOR 16)
# ===================================================================


@pytest.mark.unit
class TestEnsureScriptsDirRcContentPreservation:
    """Verify rc file pre-existing content is preserved after edit."""

    SCRIPTS_DIR = Path("/home/user/.local/bin")

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli_install.Path.exists", return_value=True)
    @patch("deile.cli_install.Path.read_text")
    @patch("deile.cli_install.tempfile.mkstemp")
    @patch("deile.cli_install.os.write")
    @patch("deile.cli_install.os.close")
    @patch("deile.cli_install.os.replace")
    @patch("deile.cli_install.Path.mkdir")
    def test_pre_existing_content_preserved(
        self,
        mock_mkdir,
        mock_replace,
        mock_close,
        mock_write,
        mock_mkstemp,
        mock_read,
        mock_exists,
        mock_resolve,
        mock_home,
        mock_environ,
    ):
        """Pre-existing .zshrc content is included in the written output."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()
        existing_content = "# my zshrc\nalias ll='ls -la'\n"
        mock_read.return_value = existing_content
        mock_mkstemp.return_value = (999, "/home/user/.deile_rc_abc123.tmp")

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)

        assert modified is True
        written_bytes = mock_write.call_args[0][1]
        written_text = written_bytes.decode("utf-8")

        # Original content must be preserved
        assert "# my zshrc" in written_text
        assert "alias ll='ls -la'" in written_text
        # New export line appended
        assert 'export PATH="/home/user/.local/bin:$PATH"' in written_text
        # Atomic rename used
        mock_replace.assert_called_once()

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install.Path.resolve", return_value=Path("/home/user/.zshrc"))
    @patch("deile.cli_install.Path.exists", return_value=False)
    @patch("deile.cli_install.Path.read_text")
    def test_path_with_double_quote_returns_hint(
        self, mock_read, mock_exists, mock_resolve, mock_home, mock_environ
    ):
        """scripts_dir with double-quote -> returns manual hint instead of writing."""
        from deile.cli_install import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user").resolve()

        bad_dir = Path('/home/user"evil/.local/bin')
        modified, rc_path, hint = _ensure_scripts_dir_on_path(bad_dir)

        assert modified is False
        assert rc_path is None
        assert "unsupported characters" in hint


# ===================================================================
# NIT 20: --install-mode without --install validation order
# ===================================================================


@pytest.mark.unit
class TestInstallModeValidationOrder:
    """--install-mode without --install must error before running install."""

    def test_install_mode_without_install_flag_errors(self):
        """--install-mode without --install -> exit 2 with error message."""
        import io
        from unittest.mock import patch as _patch

        from deile.cli import main

        captured = io.StringIO()
        with _patch("sys.stderr", captured):
            with _patch("deile.cli._run_self_install") as mock_install:
                result = main(["--install-mode", "global"])

        assert result == 2
        # _run_self_install must NOT have been called
        mock_install.assert_not_called()
        assert "--install-mode requires --install" in captured.getvalue()


# ===================================================================
# Windows-specific install branches (issue #283)
#
# The existing suite covers `os.name == "nt"` for the simpler helpers
# (_wrapper_target_dir, _ensure_scripts_dir_on_path, _link_global_command),
# but three Windows code paths slipped through:
#
#   1. `_create_venv_with_deile` builds `venv_dir / "Scripts/python.exe"`
#      and `venv_dir / "Scripts/deile.exe"` on Windows — never tested.
#   2. `_user_scripts_dir` selects the `nt_user` sysconfig scheme — only
#      tested via the (returns a Path) smoke check; the scheme-selection
#      branch is uncovered.
#   3. `_run_self_install_async` skips the `which deile` subprocess on
#      Windows via `if os.name != "nt"` — the Linux CI takes the True
#      branch every run and the False branch is dead-code in coverage.
#
# These tests close those gaps with mock-based assertions that don't
# require a Windows runner.
# ===================================================================


@pytest.mark.unit
class TestCreateVenvWithDeileWindows:
    """_create_venv_with_deile() on Windows builds Scripts\\*.exe paths.

    The function's behavior is governed by `os.name == "nt"` (deciding
    `Scripts/python.exe` vs `bin/python` and `Scripts/deile.exe` vs
    `bin/deile`). Path semantics elsewhere are flavour-agnostic — the
    security check only compares string prefixes — so POSIX-style paths
    work for the venv/home arguments and keep the test runnable on Linux
    CI.

    Caveat (and the reason we use POSIX paths here): patching
    `deile.cli.os.name = "nt"` actually mutates the global `os.name`,
    which makes `pathlib.Path()` instances constructed inside the patch
    become `WindowsPath`. Mixing `PosixPath` (constructed at class load)
    with `WindowsPath` (constructed inside the test) flips slashes and
    breaks the `str(venv).startswith(str(home))` security gate.
    """

    # POSIX-style paths — constructed at class-load (PosixPath, forward
    # slashes). At test time we don't construct any new `Path()` inside the
    # `os.name=="nt"` patch — every value passes through the mocks we set up.
    VENV_DIR = Path("/tmp/test-venv-win")
    REPO_ROOT = Path("/tmp/test-repo-win")
    HOME_DIR = Path("/tmp")  # so `str(VENV_DIR).startswith(str(HOME_DIR))` is True

    @patch("deile.cli_install.os.name", "nt")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install.Path.resolve")
    @patch("deile.cli_install.Path.exists")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.asyncio.create_subprocess_exec")
    def test_windows_venv_python_is_scripts_python_exe(
        self,
        mock_subproc,
        mock_mkdir,
        mock_exists,
        mock_resolve,
        mock_home,
    ):
        """venv_py path on Windows is `<venv>/Scripts/python.exe`, not `bin/python`."""
        from deile.cli_install import _create_venv_with_deile

        # Path.home() returns a pre-baked PosixPath so it doesn't try to
        # instantiate WindowsPath (os.name is patched to 'nt', but we're on
        # Linux). The `.resolve()` on it is mocked separately.
        mock_home.return_value = self.HOME_DIR
        # 3 `.resolve()` calls: venv_dir, repo_root, Path.home(). All class
        # constants are pre-baked PosixPath so str() keeps forward slashes
        # and the security check `startswith(home)` passes.
        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, self.HOME_DIR]
        # venv_py absent (trigger creation), requirements.txt present, deile_script present
        mock_exists.side_effect = [False, True, True]

        ok_proc = MagicMock()
        ok_proc.returncode = 0
        ok_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subproc.return_value = ok_proc

        with patch("deile.cli_install._venv.EnvBuilder"):
            with patch("deile.cli_install.asyncio.to_thread"):
                result = asyncio.run(
                    _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")
                )

        # The function returned `<venv>/Scripts/deile.exe` (NOT `bin/deile`).
        # That's the actual Windows assertion we care about.
        assert result == self.VENV_DIR / "Scripts/deile.exe"
        # And every pip invocation used `<venv>/Scripts/python.exe` as the
        # interpreter (positional arg 0 of `create_subprocess_exec`).
        expected_python = str(self.VENV_DIR / "Scripts/python.exe")
        for call in mock_subproc.call_args_list:
            assert (
                call[0][0] == expected_python
            ), f"expected venv python {expected_python!r}, got {call[0][0]!r}"

    @patch("deile.cli_install.os.name", "nt")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install.Path.resolve")
    @patch("deile.cli_install.Path.exists")
    @patch("deile.cli_install.Path.mkdir")
    @patch("deile.cli_install.asyncio.create_subprocess_exec")
    def test_windows_missing_deile_exe_raises(
        self,
        mock_subproc,
        mock_mkdir,
        mock_exists,
        mock_resolve,
        mock_home,
    ):
        """If `Scripts/deile.exe` is absent after install on Windows, raises."""
        from deile.cli_install import _create_venv_with_deile

        mock_home.return_value = self.HOME_DIR
        mock_resolve.side_effect = [self.VENV_DIR, self.REPO_ROOT, self.HOME_DIR]
        # venv_py absent, requirements.txt present, deile_script absent
        mock_exists.side_effect = [False, True, False]

        ok_proc = MagicMock()
        ok_proc.returncode = 0
        ok_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_subproc.return_value = ok_proc

        with patch("deile.cli_install._venv.EnvBuilder"):
            with patch("deile.cli_install.asyncio.to_thread"):
                with pytest.raises(
                    DEILEInstallError, match="console script not created"
                ):
                    asyncio.run(
                        _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")
                    )


@pytest.mark.unit
class TestUserScriptsDirWindowsFallback:
    """_user_scripts_dir() on old Python (<3.10) on Windows uses nt_user."""

    @patch("deile.cli_install.os.name", "nt")
    @patch("deile.cli_install.sysconfig")
    def test_nt_user_scheme_when_preferred_scheme_absent(self, mock_sysconfig):
        """Old Pythons (<3.10) lack `get_preferred_scheme` → fallback to nt_user.

        We force ``Path`` to ``PosixPath`` so that constructing a path from
        a Windows-style string does not try to instantiate ``WindowsPath``
        on the Linux CI runner (``os.name`` is patched to ``'nt'``).
        """
        from pathlib import PosixPath

        from deile.cli_install import _user_scripts_dir

        # Simulate old Python by removing `get_preferred_scheme` from sysconfig.
        del mock_sysconfig.get_preferred_scheme
        mock_sysconfig.get_path.return_value = (
            "C:/Users/test/AppData/Roaming/Python/Python310/Scripts"
        )

        with patch("deile.cli_install.Path", PosixPath):
            result = _user_scripts_dir()

        # The function must have asked sysconfig for "scripts" under "nt_user".
        mock_sysconfig.get_path.assert_called_once_with("scripts", scheme="nt_user")
        assert isinstance(result, Path)


@pytest.mark.unit
class TestRunSelfInstallAsyncWindowsSkipsWhich:
    """_run_self_install_async() must NOT run `which deile` on Windows.

    The Linux branch shells out to `/usr/bin/env which deile` to verify the
    shim is on PATH; on Windows that subprocess is meaningless and the
    `os.name != "nt"` guard skips it entirely. This regression tests the
    skip — a single inverted operator would have the install crash on
    Windows with `FileNotFoundError: /usr/bin/env`.

    Implementation note: we MUST also mock `Path.home` because patching
    `deile.cli.os.name = "nt"` actually mutates the global `os.name`
    (since `deile.cli.os` is the singleton `os` module), which makes
    `pathlib.Path()` constructors default to `WindowsPath`. Instantiating
    `WindowsPath` on a POSIX system raises `UnsupportedOperation`, so
    `Path.home()` would crash before we even reach the `which` branch.
    Returning a plain string from `_wrapper_target_dir`-replacement avoids
    further constructor work inside the patched scope.
    """

    @patch("deile.cli_install.os.name", "nt")
    @patch("deile.cli_install.Path.home")
    @patch("deile.cli_install._prompt_install_mode", return_value="global")
    @patch("deile.cli_install._create_venv_with_deile")
    @patch("deile.cli_install._wrapper_target_dir")
    @patch("deile.cli_install._link_global_command")
    @patch(
        "deile.cli_install._ensure_scripts_dir_on_path",
        return_value=(False, None, "PowerShell hint"),
    )
    @patch("deile.cli_install.asyncio.to_thread")
    @patch("builtins.print")
    def test_which_subprocess_not_called_on_windows(
        self,
        mock_print,
        mock_to_thread,
        mock_ensure_path,
        mock_link,
        mock_wrapper_dir,
        mock_create_venv,
        mock_prompt,
        mock_home,
    ):
        from deile.cli_install import _run_self_install_async

        # `Path.home()` is normally called twice inside the function chain;
        # return a plain MagicMock whose `__truediv__` chains transparently.
        # The actual returned paths are irrelevant for this assertion — we
        # only care that `to_thread` (= the which subprocess) is NOT called.
        home_mock = MagicMock(spec=Path)
        home_mock.__truediv__.return_value = home_mock
        mock_home.return_value = home_mock

        wrapper_target = MagicMock(spec=Path)
        wrapper_target.name = "Scripts"
        mock_wrapper_dir.return_value = wrapper_target

        deile_script = MagicMock(spec=Path)
        deile_script.parent.parent = MagicMock()
        mock_create_venv.return_value = deile_script

        link_target = MagicMock(spec=Path)
        link_target.name = "deile.cmd"
        link_target.parent = MagicMock()
        mock_link.return_value = link_target

        result = asyncio.run(_run_self_install_async(mode=None))

        assert result == 0
        # `asyncio.to_thread` is what wraps `subprocess.run(['/usr/bin/env',
        # 'which', 'deile'])`. On Windows that call MUST be skipped — if
        # `to_thread` was invoked, the `which` branch ran by mistake.
        mock_to_thread.assert_not_called()

    @patch("deile.cli_install.os.name", "posix")
    @patch("deile.cli_install._prompt_install_mode", return_value="global")
    @patch("deile.cli_install._create_venv_with_deile")
    @patch("deile.cli_install._wrapper_target_dir")
    @patch("deile.cli_install._link_global_command")
    @patch(
        "deile.cli_install._ensure_scripts_dir_on_path", return_value=(False, None, "")
    )
    @patch("deile.cli_install.asyncio.to_thread")
    @patch("builtins.print")
    def test_which_subprocess_called_on_posix_for_contrast(
        self,
        mock_print,
        mock_to_thread,
        mock_ensure_path,
        mock_link,
        mock_wrapper_dir,
        mock_create_venv,
        mock_prompt,
    ):
        """Negative control: same scenario on POSIX MUST run `which deile`."""
        from deile.cli_install import _run_self_install_async

        mock_create_venv.return_value = Path("/home/user/.deile/venv/bin/deile")
        mock_wrapper_dir.return_value = Path("/home/user/.local/bin")
        mock_link.return_value = Path("/home/user/.local/bin/deile")

        # `to_thread` returns an awaitable, so we have to return a coroutine.
        async def _fake_to_thread(*args, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="")

        mock_to_thread.side_effect = _fake_to_thread

        result = asyncio.run(_run_self_install_async(mode=None))

        assert result == 0
        # On POSIX `to_thread` IS called — proves the contrast assertion
        # above is meaningful.
        mock_to_thread.assert_called_once()
        # Inspect the subprocess command — must be `/usr/bin/env which deile`.
        call_args = mock_to_thread.call_args[0]
        # Signature: (subprocess.run, ['/usr/bin/env', 'which', 'deile'], ...)
        assert call_args[1] == ["/usr/bin/env", "which", "deile"]
