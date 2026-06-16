"""Tests for _env_file_has_valid_key (deile.py) and _run_env_recovery (deile/cli.py).

Imports:
  * deile.py (root script)  → importlib.util.spec_from_file_location
  * deile.cli               → direct import from the installed package

Fixtures and mocks:
  * tmp_path           → pytest built-in for all temporary files
  * sys.stdin.isatty   → control TTY detection
  * getpass.getpass    → control user input or raise KeyboardInterrupt/EOFError
  * os.chmod           → no-op (permissions irrelevant in test)
  * dotenv.load_dotenv → no-op (env loading irrelevant in test)
  * _find_dotenv       → control where .env is located
  * _PROJECT_ROOT      → patched to tmp_path when needed
  * ENV_FILE           → patched to tmp_path/.env for the root-script function

No asyncio, no real API calls, no side effects beyond the temp directory.

NOTE on @patch targets:
  * _run_env_recovery() does `import getpass` INSIDE the function — so
    `getpass` is NOT an attribute of `deile.cli`. We patch "getpass.getpass"
    (the stdlib module) directly.
  * Same for `from dotenv import load_dotenv` inside the function — patch
    "dotenv.load_dotenv".
  * Items imported at the top of deile/cli.py (sys, os, deile.cli._find_dotenv)
    can be patched as "deile.cli.<attr>".
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_deile_root():
    """Import deile.py (root script) via importlib and return the module."""
    root_script = Path(__file__).resolve().parent.parent.parent / "deile.py"
    spec = importlib.util.spec_from_file_location("deile_root", str(root_script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests for _env_file_has_valid_key  (deile.py - root script)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnvFileHasValidKey:
    """_env_file_has_valid_key() — imported via importlib from deile.py

    Cases:
      1. Arquivo inexistente → False
      2. Arquivo vazio       → False
      3. Só comentários      → False
      4. Chave valor vazio   → False
      5. Chave valor preenchido → True
      6. Várias chaves, uma válida → True
      7. Apenas chaves não reconhecidas → False
      8. Espaços extras no nome da chave → True (k.strip())
      9. Linha sem `=` → ignorada → False
    """

    @pytest.fixture
    def deile_mod(self):
        """Import deile.py root script once per test."""
        return _import_deile_root()

    @pytest.fixture
    def env_file(self, tmp_path, deile_mod):
        """Patch deile_mod.ENV_FILE to point into tmp_path and yield the path."""
        env = tmp_path / ".env"
        with patch.object(deile_mod, "ENV_FILE", env):
            yield env

    def test_file_not_found_returns_false(self, deile_mod, env_file):
        """Case 1: arquivo inexistente retorna False."""
        assert not env_file.exists()
        assert deile_mod._env_file_has_valid_key() is False

    def test_empty_file_returns_false(self, deile_mod, env_file):
        """Case 2: arquivo vazio retorna False."""
        env_file.write_text("", encoding="utf-8")
        assert deile_mod._env_file_has_valid_key() is False

    def test_only_comments_returns_false(self, deile_mod, env_file):
        """Case 3: só comentários retorna False."""
        env_file.write_text(
            "# DEILE configuration file\n" "# another comment line\n",
            encoding="utf-8",
        )
        assert deile_mod._env_file_has_valid_key() is False

    def test_key_with_empty_value_returns_false(self, deile_mod, env_file):
        """Case 4: chave com valor vazio retorna False."""
        env_file.write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
        assert deile_mod._env_file_has_valid_key() is False

    def test_key_with_valid_value_returns_true(self, deile_mod, env_file):
        """Case 5: chave com valor preenchido retorna True."""
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-12345\n",
            encoding="utf-8",
        )
        assert deile_mod._env_file_has_valid_key() is True

    def test_mixed_keys_with_valid_returns_true(self, deile_mod, env_file):
        """Case 6: várias chaves, uma com valor válido → True."""
        env_file.write_text(
            "OPENAI_API_KEY=\n" "DEEPSEEK_API_KEY=ds-secret-456\n" "GOOGLE_API_KEY=\n",
            encoding="utf-8",
        )
        assert deile_mod._env_file_has_valid_key() is True

    def test_only_unrecognized_keys_returns_false(self, deile_mod, env_file):
        """Case 7: apenas chaves não reconhecidas → False."""
        env_file.write_text(
            "MY_CUSTOM_KEY=abc\nSOME_OTHER=xyz\n",
            encoding="utf-8",
        )
        assert deile_mod._env_file_has_valid_key() is False

    def test_spaces_around_key_name_returns_true(self, deile_mod, env_file):
        """Case 8: espaços extras no nome da chave (k.strip()) → True."""
        env_file.write_text(
            "  ANTHROPIC_API_KEY  =  sk-ant-xyz  \n",
            encoding="utf-8",
        )
        assert deile_mod._env_file_has_valid_key() is True

    def test_line_without_equals_returns_false(self, deile_mod, env_file):
        """Case 9: linha sem `=` é ignorada → False."""
        env_file.write_text(
            "ANTHROPIC_API_KEY\n",
            encoding="utf-8",
        )
        assert deile_mod._env_file_has_valid_key() is False


# ---------------------------------------------------------------------------
# Tests for _run_env_recovery  (deile/cli.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunEnvRecovery:
    """_run_env_recovery() — imported from deile.cli

    Cases:
      1. stdin não é TTY              → False
      2. KeyboardInterrupt            → False
      3. EOFError                     → False
      4. Zero chaves inseridas        → False
      5. Uma chave inserida           → True + .env criado + load_dotenv chamado
      6. Merge preserva DEILE_* vars  → True + .env mergeado
      7. Valor de env existente preservado quando usuário entra vazio → True
      8. Fallback sem dotenv          → True + os.environ setado
      9. _find_dotenv path custom     → True + .env no path custom

    Patching notes:
      * getpass.getpass  → patch "getpass.getpass" (import INSIDE function)
      * dotenv.load_dotenv → patch "dotenv.load_dotenv" (import INSIDE function)
      * sys.stdin.isatty → patch "deile.cli.sys.stdin.isatty"
      * os.chmod / os.getenv → patch "deile.cli.os.chmod" / "deile.cli.os.getenv"
      * _find_dotenv     → patch "deile.cli._find_dotenv"
      * print            → patch "builtins.print"
      * _PROJECT_ROOT    → patch.object(cli_mod, "_PROJECT_ROOT", ...)
    """

    @pytest.fixture(autouse=True)
    def _clean_environ(self):
        """Remove API-key env vars before each test; restore on teardown.

        Prevents real environment variables from leaking into tests and
        isolates os.environ mutations between test methods.
        """
        saved = dict(os.environ)
        for k in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "GOOGLE_API_KEY",
        ):
            os.environ.pop(k, None)
        yield
        os.environ.clear()
        os.environ.update(saved)

    # -- 1. stdin not TTY --

    @patch("deile.cli.sys.stdin.isatty", return_value=False)
    def test_not_tty_returns_false(self, mock_isatty):
        """Case 1: stdin não é TTY → False."""
        from deile.cli import _run_env_recovery

        assert _run_env_recovery() is False

    # -- 2. KeyboardInterrupt --

    @patch("deile.cli.sys.stdin.isatty", return_value=True)
    @patch("getpass.getpass", side_effect=KeyboardInterrupt)
    @patch("builtins.print")
    def test_keyboard_interrupt_returns_false(
        self,
        mock_print,
        mock_getpass,
        mock_isatty,
    ):
        """Case 2: usuário cancela com KeyboardInterrupt → False."""
        from deile.cli import _run_env_recovery

        assert _run_env_recovery() is False

    # -- 3. EOFError --

    @patch("deile.cli.sys.stdin.isatty", return_value=True)
    @patch("getpass.getpass", side_effect=EOFError)
    @patch("builtins.print")
    def test_eof_error_returns_false(
        self,
        mock_print,
        mock_getpass,
        mock_isatty,
    ):
        """Case 3: usuário cancela com EOFError → False."""
        from deile.cli import _run_env_recovery

        assert _run_env_recovery() is False

    # -- 4. zero keys inserted --

    @patch("deile.cli.sys.stdin.isatty", return_value=True)
    @patch("getpass.getpass", return_value="")
    @patch("deile.cli.os.getenv", return_value="")
    @patch("builtins.print")
    def test_zero_keys_returns_false(
        self,
        mock_print,
        mock_getenv,
        mock_getpass,
        mock_isatty,
    ):
        """Case 4: todas as entradas vazias e sem env var → False."""
        from deile.cli import _run_env_recovery

        assert _run_env_recovery() is False

    # -- 5. one key inserted --

    @patch("deile.cli.sys.stdin.isatty", return_value=True)
    @patch("deile.cli._find_dotenv", return_value=None)
    @patch("deile.cli.os.chmod")
    @patch("builtins.print")
    def test_one_key_writes_env_and_returns_true(
        self,
        mock_print,
        mock_chmod,
        mock_find,
        mock_isatty,
        tmp_path,
    ):
        """Case 5: uma chave inserida → .env criado, load_dotenv, True."""
        import deile.cli as _cli

        env_path = tmp_path / ".env"
        with patch.object(_cli, "_PROJECT_ROOT", tmp_path):
            with patch("getpass.getpass") as gp:
                gp.side_effect = [
                    "sk-ant-secret123",  # ANTHROPIC_API_KEY ✓
                    "",  # OPENAI_API_KEY     (empty)
                    "",  # DEEPSEEK_API_KEY   (empty)
                    "",  # GOOGLE_API_KEY     (empty)
                ]
                with patch("dotenv.load_dotenv") as _:
                    result = _cli._run_env_recovery()

        assert result is True
        assert env_path.exists()
        text = env_path.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-ant-secret123" in text
        # Empty-value keys must NOT appear in final file (filtered by `if v`)
        for line in text.splitlines():
            assert not line.startswith(
                "OPENAI_API_KEY="
            ), f"OPENAI_API_KEY should not appear (empty value): {line!r}"
            assert not line.startswith(
                "DEEPSEEK_API_KEY="
            ), f"DEEPSEEK_API_KEY should not appear (empty value): {line!r}"
            assert not line.startswith(
                "GOOGLE_API_KEY="
            ), f"GOOGLE_API_KEY should not appear (empty value): {line!r}"

    # -- 6. merge preserves DEILE_* vars --

    @patch("deile.cli.sys.stdin.isatty", return_value=True)
    @patch("deile.cli._find_dotenv", return_value=None)
    @patch("deile.cli.os.chmod")
    @patch("builtins.print")
    def test_merge_preserves_deile_vars(
        self,
        mock_print,
        mock_chmod,
        mock_find,
        mock_isatty,
        tmp_path,
    ):
        """Case 6: .env existente com DEILE_* vars e chave antiga é mergeado."""
        import deile.cli as _cli

        env_path = tmp_path / ".env"
        env_path.write_text(
            "DEILE_LOG_LEVEL=debug\n"
            "ANTHROPIC_API_KEY=sk-old-key\n"
            "DEILE_THEME=dark\n"
            "# a comment line\n"
            "GOOGLE_API_KEY=\n"
            "\n",  # blank line
            encoding="utf-8",
        )

        with patch.object(_cli, "_PROJECT_ROOT", tmp_path):
            with patch("getpass.getpass") as gp:
                gp.side_effect = [
                    "sk-new-key",  # ANTHROPIC_API_KEY → new value
                    "",  # OPENAI_API_KEY     (empty)
                    "",  # DEEPSEEK_API_KEY   (empty)
                    "g-new-key",  # GOOGLE_API_KEY     → new value
                ]
                with patch("dotenv.load_dotenv") as _:
                    result = _cli._run_env_recovery()

        assert result is True
        text = env_path.read_text(encoding="utf-8")

        assert "DEILE_LOG_LEVEL=debug" in text
        assert "DEILE_THEME=dark" in text
        assert "# a comment line" in text
        assert "\n\n" in text or text.endswith("\n\n")
        assert "ANTHROPIC_API_KEY=sk-new-key" in text
        assert "GOOGLE_API_KEY=g-new-key" in text
        assert "sk-old-key" not in text  # old value replaced

    # -- 7. existing env var preserved when user enters empty --

    @patch("deile.cli.sys.stdin.isatty", return_value=True)
    @patch("deile.cli._find_dotenv", return_value=None)
    @patch("deile.cli.os.chmod")
    @patch("builtins.print")
    def test_preserves_current_env_value_on_empty_input(
        self,
        mock_print,
        mock_chmod,
        mock_find,
        mock_isatty,
        tmp_path,
    ):
        """Case 7: entrada vazia + env var existente → valor preservado.

        Simula o cenário em que o .env já foi carregado por _load_dotenv()
        antes de _run_env_recovery() ser chamado — a variável está em
        os.environ. Quando o usuário aperta ENTER (vazio), a função usa
        `val or current` e preserva o valor corrente.
        """
        import deile.cli as _cli

        os.environ["ANTHROPIC_API_KEY"] = "sk-env-ant-789"

        env_path = tmp_path / ".env"
        with patch.object(_cli, "_PROJECT_ROOT", tmp_path):
            with patch("getpass.getpass") as gp:
                gp.side_effect = [
                    "",  # ANTHROPIC → empty → keep current
                    "",  # OPENAI
                    "",  # DEEPSEEK
                    "",  # GOOGLE
                ]
                with patch("dotenv.load_dotenv") as _:
                    result = _cli._run_env_recovery()

        assert result is True
        assert env_path.exists()
        text = env_path.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-env-ant-789" in text

    # -- 8. fallback sets os.environ directly when dotenv is unavailable --

    @patch("deile.cli.sys.stdin.isatty", return_value=True)
    @patch("deile.cli._find_dotenv", return_value=None)
    @patch("deile.cli.os.chmod")
    @patch("builtins.print")
    def test_fallback_sets_os_environ_when_dotenv_missing(
        self,
        mock_print,
        mock_chmod,
        mock_find,
        mock_isatty,
        tmp_path,
    ):
        """Case 8: dotenv não disponível → fallback seta os.environ diretamente."""
        import deile.cli as _cli

        env_path = tmp_path / ".env"
        with patch.object(_cli, "_PROJECT_ROOT", tmp_path):
            with patch("getpass.getpass") as gp:
                gp.side_effect = [
                    "sk-fallback-001",
                    "",
                    "",
                    "",
                ]
                with patch(
                    "dotenv.load_dotenv",
                    side_effect=ImportError("no dotenv"),
                ) as _:
                    result = _cli._run_env_recovery()

        assert result is True
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-fallback-001"
        assert env_path.exists()
        text = env_path.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-fallback-001" in text

    # -- 9. _find_dotenv returns a custom path --

    @patch("deile.cli.sys.stdin.isatty", return_value=True)
    @patch("deile.cli.os.chmod")
    @patch("builtins.print")
    def test_custom_dotenv_path_is_used(
        self,
        mock_print,
        mock_chmod,
        mock_isatty,
        tmp_path,
    ):
        """Case 9: _find_dotenv aponta path custom → .env escrito lá."""
        import deile.cli as _cli

        custom_env = tmp_path / "custom" / ".env"
        custom_env.parent.mkdir(parents=True)

        with patch("deile.cli._find_dotenv", return_value=custom_env):
            with patch("getpass.getpass") as gp:
                gp.side_effect = [
                    "sk-custom-path-key",
                    "",
                    "",
                    "",
                ]
                with patch("dotenv.load_dotenv") as _:
                    result = _cli._run_env_recovery()

        assert result is True
        assert custom_env.exists(), f".env should have been created at {custom_env}"
        text = custom_env.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-custom-path-key" in text
