"""Tests for the isolated-venv install functions in deile/cli.py.

Functions under test:
  * _ensure_scripts_dir_on_path(scripts_dir)
  * _user_scripts_dir()
  * _wrapper_target_dir()
  * _create_venv_with_deile(venv_dir, repo_root, mode_label)
  * _link_global_command(target_dir, source_script, *, force=False)
  * _prompt_install_mode()
  * _run_self_install(mode=None)

No asyncio, no real API calls, no side effects beyond the temp directory.
Venv creation is mocked via unittest.mock — we never call real `venv` or `pip`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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

        mock_user_scripts.return_value = Path("C:/Users/test/AppData/Roaming/Python/Scripts")
        result = _wrapper_target_dir()
        assert result == mock_user_scripts.return_value


# ===================================================================
# _ensure_scripts_dir_on_path
# ===================================================================

@pytest.mark.unit
class TestEnsureScriptsDirOnPath:
    """_ensure_scripts_dir_on_path() — shell detection and rc file editing.

    Returns tuple[bool, Optional[Path], str].
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

    # -- Zsh: already configured --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("deile.cli.Path.read_text")
    def test_zsh_already_configured(self, mock_read, mock_exists, mock_home,
                                     mock_environ):
        """Zsh with scripts_dir already in .zshrc -> (False, rc, '')."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user")
        mock_read.return_value = 'export PATH="/home/user/.local/bin:$PATH"\n'

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path == Path("/home/user/.zshrc")
        assert hint == ""

    # -- Bash (Linux): edits .bashrc --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.sys.platform", "linux")
    @patch("deile.cli.os.environ.get", return_value="/bin/bash")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.read_text")
    @patch("deile.cli.Path.write_text")
    @patch("deile.cli.Path.mkdir")
    def test_bash_linux_edits_bashrc(self, mock_mkdir, mock_write, mock_read,
                                      mock_exists, mock_home, mock_environ):
        """Bash on Linux -> edits .bashrc with export line."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user")
        mock_read.return_value = ""

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.bashrc")
        assert hint == ""

        # Verify the marker and export line were written
        written = mock_write.call_args[0][0]
        assert "# Added by `deile --install`" in written
        assert self.EXPORT_LINE in written

    # -- Bash (macOS): edits .bash_profile --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.sys.platform", "darwin")
    @patch("deile.cli.os.environ.get", return_value="/bin/bash")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.read_text")
    @patch("deile.cli.Path.write_text")
    @patch("deile.cli.Path.mkdir")
    def test_bash_macos_edits_bash_profile(self, mock_mkdir, mock_write,
                                            mock_read, mock_exists, mock_home,
                                            mock_environ):
        """Bash on macOS -> edits .bash_profile with export line."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user")
        mock_read.return_value = ""

        modified, rc_path, _hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.bash_profile")

    # -- Fish: edits config.fish --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/usr/bin/fish")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.read_text")
    @patch("deile.cli.Path.write_text")
    @patch("deile.cli.Path.mkdir")
    def test_fish_edits_config_fish(self, mock_mkdir, mock_write, mock_read,
                                     mock_exists, mock_home, mock_environ):
        """Fish shell -> edits config.fish with set -gx PATH."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user")
        mock_read.return_value = ""

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is True
        assert rc_path == Path("/home/user/.config/fish/config.fish")
        assert "set -gx PATH" in mock_write.call_args[0][0]

    # -- Read error --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("deile.cli.Path.read_text", side_effect=OSError("Permission denied"))
    def test_read_error_returns_hint(self, mock_read, mock_exists, mock_home,
                                      mock_environ):
        """OSError on read -> (False, rc, hint)."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user")

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path == Path("/home/user/.zshrc")
        assert "Could not read" in hint

    # -- Write error --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.os.environ.get", return_value="/bin/zsh")
    @patch("deile.cli.Path.home")
    @patch("deile.cli.Path.exists", return_value=False)
    @patch("deile.cli.Path.read_text")
    @patch("deile.cli.Path.write_text", side_effect=OSError("Read-only"))
    @patch("deile.cli.Path.mkdir")
    def test_write_error_returns_hint(self, mock_mkdir, mock_write, mock_read,
                                       mock_exists, mock_home, mock_environ):
        """OSError on write -> (False, rc, hint)."""
        from deile.cli import _ensure_scripts_dir_on_path

        mock_home.return_value = Path("/home/user")
        mock_read.return_value = ""

        modified, rc_path, hint = _ensure_scripts_dir_on_path(self.SCRIPTS_DIR)
        assert modified is False
        assert rc_path == Path("/home/user/.zshrc")
        assert "Could not write" in hint


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
    @patch("deile.cli.os.readlink", return_value="/old/path")
    @patch("builtins.input", return_value="y")
    def test_posix_force_overwrite(self, mock_input, mock_readlink,
                                    mock_symlink, mock_unlink,
                                    mock_exists, mock_is_symlink, mock_mkdir):
        """POSIX: existing symlink removed and recreated when user agrees."""
        from deile.cli import _link_global_command

        link_target = _link_global_command(self.TARGET_DIR, self.SOURCE)
        assert link_target == self.TARGET_DIR / "deile"
        mock_unlink.assert_called_once()
        mock_symlink.assert_called_once_with(self.SOURCE)

    # -- POSIX: refuses overwrite --

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.Path.mkdir")
    @patch("deile.cli.Path.is_symlink", return_value=True)
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("builtins.input", return_value="n")
    def test_posix_refuses_overwrite(self, mock_input, mock_exists,
                                      mock_is_symlink, mock_mkdir):
        """POSIX: user says no -> RuntimeError."""
        from deile.cli import _link_global_command

        with pytest.raises(RuntimeError, match="refusing to overwrite"):
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
# _create_venv_with_deile  (mocked venv + subprocess)
# ===================================================================

@pytest.mark.unit
class TestCreateVenvWithDeile:
    """_create_venv_with_deile() — mocks venv creation and pip subprocesses."""

    VENV_DIR = Path("/tmp/test-venv")
    REPO_ROOT = Path("/tmp/test-repo")

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.Path.exists")
    @patch("deile.cli.Path.mkdir")
    @patch("deile.cli.subprocess.run")
    def test_creates_venv_and_installs(self, mock_run, mock_mkdir,
                                        mock_exists):
        """Full pipeline: create venv -> upgrade pip -> install reqs -> editable."""
        from deile.cli import _create_venv_with_deile

        # _create_venv_with_deile calls .exists() 3 times:
        #   1. venv_py.exists()      -> False (trigger creation)
        #   2. requirements.exists() -> True  (pip install -r)
        #   3. deile_script.exists() -> True  (verify script was created)
        mock_exists.side_effect = [False, True, True]

        # subprocess.run is called 3 times; all must return success (rc=0)
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        deile_script = self.VENV_DIR / "bin/deile"

        # Mock the actual _venv.EnvBuilder.create
        with patch("deile.cli._venv.EnvBuilder") as mock_env_builder:
            mock_env_builder_instance = MagicMock()
            mock_env_builder.return_value = mock_env_builder_instance

            result = _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")

        # Verify venv was created
        mock_env_builder_instance.create.assert_called_once_with(str(self.VENV_DIR))

        # Verify subprocess calls
        pip_calls = [c[0][0] for c in mock_run.call_args_list]
        assert len(pip_calls) >= 3  # upgrade pip, -r requirements, --no-deps -e

        # First: upgrade pip
        assert "--upgrade" in str(pip_calls[0])
        assert "pip" in str(pip_calls[0])

        # Second: install requirements.txt
        assert "-r" in str(pip_calls[1])

        # Third: editable install without deps
        assert "--no-deps" in str(pip_calls[2])
        assert "-e" in str(pip_calls[2])

        assert result == deile_script

    @patch("deile.cli.os.name", "posix")
    @patch("deile.cli.Path.exists", return_value=True)
    @patch("deile.cli.subprocess.run")
    def test_reuses_existing_venv(self, mock_run, mock_exists):
        """If venv python already exists, skips creation."""
        from deile.cli import _create_venv_with_deile

        # subprocess.run is called 3 times; all must return success (rc=0)
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        with patch("deile.cli._venv.EnvBuilder") as mock_env_builder:
            _create_venv_with_deile(self.VENV_DIR, self.REPO_ROOT, "test")

        # EnvBuilder.create should NOT be called
        mock_env_builder.assert_not_called()
        pip_calls = [c[0][0] for c in mock_run.call_args_list]
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
        # ensure_scripts_dir_on_path should be called since which deile won't match
        mock_ensure_path.assert_called_once()

    @patch("deile.cli._prompt_install_mode", return_value=None)
    @patch("builtins.print")
    def test_cancelled_returns_1(self, mock_print, mock_prompt):
        """User cancels prompt -> returns 1."""
        from deile.cli import _run_self_install

        assert _run_self_install(mode=None) == 1

    @patch("deile.cli._create_venv_with_deile",
           side_effect=RuntimeError("venv creation failed"))
    @patch("builtins.print")
    def test_venv_error_returns_1(self, mock_print, mock_create_venv):
        """RuntimeError from _create_venv_with_deile -> returns 1."""
        from deile.cli import _run_self_install

        assert _run_self_install(mode="global") == 1

    @patch("deile.cli._create_venv_with_deile")
    @patch("deile.cli._wrapper_target_dir")
    @patch("deile.cli._link_global_command",
           side_effect=RuntimeError("refusing to overwrite"))
    @patch("builtins.print")
    def test_link_error_returns_1(
        self, mock_print, mock_link, mock_wrapper_dir, mock_create_venv,
    ):
        """RuntimeError from _link_global_command -> returns 1."""
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
