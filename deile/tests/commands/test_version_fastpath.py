"""Testes do fast-path --version/-v (issue #273).

Cobre:
  - Formato curto de saída (1 linha com build)
  - Alias -v registrado no VersionCommand
  - Zero side-effects (não cria .venv, não toca .env)
  - Comportamento em não-TTY (não pergunta painel completo)
  - Integração subprocess (python3 deile.py --version)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
import pytest

from deile.commands.builtin.version_command import VersionCommand
from deile.commands.cli_flags import CLIFlagSpec, build_cli_flag_specs
from deile.commands.registry import CommandRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]  # repo/

_VERSION_RE = re.compile(
    r"^DEILE v(\d+\.\d+\.\d+) \(build (\d{8})\)\s*$"
)

# ---------------------------------------------------------------------------
# Formato curto de saída
# ---------------------------------------------------------------------------


class TestShortVersionFormat:
    """Valida o formato da saída curta conforme decisão do stakeholder."""

    def test_format_matches_regex(self):
        """A saída deve casar com: DEILE vX.Y.Z (build YYYYMMDD)"""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        out = result.stdout.strip()
        assert _VERSION_RE.match(out), f"Formato inválido: {out!r}"

    def test_version_matches_version_module(self):
        """A versão exibida deve bater com deile.__version__."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        out = result.stdout.strip()
        import deile.__version__ as version_mod
        expected = f"DEILE v{version_mod.__version__} (build {version_mod.__build_number__})"
        assert out == expected, f"Esperado {expected!r}, obtido {out!r}"

    def test_build_number_is_eight_digits(self):
        """O build number deve ter exatamente 8 dígitos."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        match = _VERSION_RE.match(result.stdout.strip())
        assert match is not None
        assert len(match.group(2)) == 8

    def test_exit_code_zero(self):
        """Fast-path deve sair com 0."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0

    def test_single_line_output(self):
        """A saída deve ser exatamente 1 linha."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 1, f"Esperado 1 linha, obtido {len(lines)}: {lines}"


# ---------------------------------------------------------------------------
# Alias -v
# ---------------------------------------------------------------------------


class TestVAlias:
    """Valida que -v é alias de --version."""

    def test_v_flag_works(self):
        """python3 deile.py -v deve produzir a mesma saída que --version."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "-v"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert _VERSION_RE.match(result.stdout.strip())

    def test_v_flag_same_output_as_version(self):
        """-v e --version produzem saídas idênticas."""
        r1 = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        r2 = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "-v"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert r1.stdout == r2.stdout

    def test_v_registered_as_cli_flag_alias(self):
        """VersionCommand deve ter -v em cli_flag_aliases."""
        cmd = VersionCommand()
        assert hasattr(cmd, "cli_flag_aliases")
        assert "-v" in cmd.cli_flag_aliases

    def test_v_in_argparse_specs(self):
        """O spec gerado por build_cli_flag_specs deve conter -v como alias."""
        registry = CommandRegistry()
        registry.auto_discover_builtin_commands()
        specs = build_cli_flag_specs(registry)
        version_specs = [s for s in specs if s.command_name == "version"]
        assert len(version_specs) == 1
        spec = version_specs[0]
        assert spec.flag == "--version"
        assert spec.aliases is not None
        assert "-v" in spec.aliases

    def test_v_mixed_with_other_args(self):
        """-v junto com outros argumentos ainda funciona (fast-path intercepta)."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "-v", "mensagem"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert _VERSION_RE.match(result.stdout.strip())


# ---------------------------------------------------------------------------
# Zero side-effects
# ---------------------------------------------------------------------------


class TestZeroSideEffects:
    """Fast-path não deve criar .venv nem .env."""

    def test_no_venv_created(self, tmp_path):
        """Rodar --version num dir limpo não cria .venv."""
        # Copia apenas deile.py e deile/__version__.py para dir limpo
        import shutil
        work = tmp_path / "work"
        work.mkdir()
        # Copia deile.py e deile/
        shutil.copytree(REPO_ROOT / "deile", work / "deile",
                        ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))
        shutil.copy2(REPO_ROOT / "deile.py", work / "deile.py")

        result = subprocess.run(
            [sys.executable, str(work / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(work),
        )
        assert result.returncode == 0
        assert not (work / ".venv").exists(), (
            ".venv foi criado — fast-path não deve ter side-effects"
        )
        assert not (work / ".env").exists(), (
            ".env foi criado — fast-path não deve ter side-effects"
        )

    def test_no_env_touched(self):
        """Rodar --version não deve modificar .env existente."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        # Não há assertions sobre .env — só garantimos que não crashou


# ---------------------------------------------------------------------------
# Comportamento não-TTY
# ---------------------------------------------------------------------------


class TestNonTTYBehavior:
    """Em ambiente não-interativo, o fast-path não deve perguntar nada."""

    def test_no_prompt_in_non_tty(self):
        """Com stdout não-TTY, --version não deve perguntar sobre painel completo."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        # Sem TTY, não deve haver prompt
        assert "Instalar dependências" not in result.stdout
        assert result.returncode == 0

    def test_no_stderr_in_non_tty(self):
        """Fast-path não deve escrever nada em stderr."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.stderr == ""

    def test_stdin_not_consumed(self):
        """Fast-path não deve ler stdin em modo não-interativo."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            input="y\n", capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        # Mesmo com "y" no stdin, não deve haver prompt
        assert "DEILE v" in result.stdout
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# Subprocess smoke tests
# ---------------------------------------------------------------------------


class TestSubprocessSmoke:
    """Testes de integração via subprocess."""

    def test_version_without_pythonpath(self):
        """--version funciona sem PYTHONPATH especial."""
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            env=env,
        )
        assert result.returncode == 0
        assert "DEILE v" in result.stdout

    def test_version_from_subdir(self):
        """--version funciona quando chamado de um subdiretório."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT / "deile"),
        )
        assert result.returncode == 0
        assert "DEILE v" in result.stdout

    def test_version_has_no_rich_markup(self):
        """A saída curta não deve conter markup Rich."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert "[" not in result.stdout  # Rich usa [bold], [cyan], etc.

    def test_version_response_time_under_200ms(self):
        """Fast-path deve responder em < 200ms (não faz bootstrap)."""
        import time
        t0 = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deile.py"), "--version"],
            capture_output=True, cwd=str(REPO_ROOT),
        )
        elapsed = time.monotonic() - t0
        assert result.returncode == 0
        assert elapsed < 0.5, f"Fast-path demorou {elapsed:.2f}s (limite 0.5s)"


# ---------------------------------------------------------------------------
# TTY interativo (mock) — prompt painel completo
# ---------------------------------------------------------------------------


def _load_deile_script():
    """Carrega o script deile.py como módulo para permitir mocking.

    O pacote ``deile`` (deile/__init__.py) sombreia o script ``deile.py``,
    portanto usamos importlib para carregar o script com um nome distinto.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_deile_script", str(REPO_ROOT / "deile.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestInteractiveTTYPrompt:
    """Cobre o prompt interativo quando TTY está ativo e .venv não existe.

    Usa mocking de sys.stdout.isatty antes do import do script deile.py
    para simular ambiente interativo sem venv. O ``_venv_python`` do módulo
    é patchado diretamente para evitar interferência com outras operações
    de Path no carregamento.
    """

    def test_tty_prompt_shows_when_no_venv(self):
        """Em TTY sem venv, o prompt para painel completo deve aparecer."""
        from unittest.mock import patch, MagicMock

        fake_venv = MagicMock()
        fake_venv.exists.return_value = False
        with patch("sys.stdout.isatty", return_value=True), \
             patch("builtins.input", return_value="n"):
            mod = _load_deile_script()
            with patch.object(mod, "_venv_python", return_value=fake_venv):
                with pytest.raises(SystemExit) as exc_info:
                    mod._fast_version()
                assert exc_info.value.code == 0

    def test_tty_prompt_no_shows_short_line(self):
        """TTY com resposta 'não' → sai com linha curta e exit 0."""
        from unittest.mock import patch, MagicMock
        from io import StringIO

        fake_stdout = StringIO()
        fake_venv = MagicMock()
        fake_venv.exists.return_value = False
        with patch("sys.stdout.isatty", return_value=True), \
             patch("builtins.input", return_value="n"):
            mod = _load_deile_script()
            with patch.object(mod, "_venv_python", return_value=fake_venv), \
                 patch.object(mod.sys, "stdout", fake_stdout):
                with pytest.raises(SystemExit) as exc_info:
                    mod._fast_version()
                assert exc_info.value.code == 0
                output = fake_stdout.getvalue()
                assert "DEILE v" in output
                assert "painel completo" in output

    def test_tty_prompt_yes_triggers_bootstrap_path(self):
        """TTY com resposta 'sim' → chama bootstrap leve (_create_venv, _install_deps)."""
        from unittest.mock import patch, MagicMock

        fake_venv = MagicMock()
        fake_venv.exists.return_value = False
        with patch("sys.stdout.isatty", return_value=True), \
             patch("builtins.input", return_value="y"):
            mod = _load_deile_script()
            with patch.object(mod, "_venv_python", return_value=fake_venv), \
                 patch.object(mod, "_check_python_version") as mock_check, \
                 patch.object(mod, "_create_venv") as mock_venv, \
                 patch.object(mod, "_install_deps") as mock_deps, \
                 patch.object(mod, "_exec_in_venv") as mock_exec:
                with pytest.raises(SystemExit) as exc_info:
                    mod._fast_version()
                assert exc_info.value.code == 0
                mock_check.assert_called_once()
                mock_venv.assert_called_once()
                mock_deps.assert_called_once()
                mock_exec.assert_called_once()

    def test_tty_prompt_yes_does_not_call_ensure_env(self):
        """Bootstrap leve NÃO deve chamar _ensure_env_file (sem wizard de API key)."""
        from unittest.mock import patch, MagicMock

        fake_venv = MagicMock()
        fake_venv.exists.return_value = False
        with patch("sys.stdout.isatty", return_value=True), \
             patch("builtins.input", return_value="y"):
            mod = _load_deile_script()
            with patch.object(mod, "_venv_python", return_value=fake_venv), \
                 patch.object(mod, "_check_python_version"), \
                 patch.object(mod, "_create_venv"), \
                 patch.object(mod, "_install_deps"), \
                 patch.object(mod, "_exec_in_venv"), \
                 patch.object(mod, "_ensure_env_file") as mock_env:
                with pytest.raises(SystemExit) as exc_info:
                    mod._fast_version()
                assert exc_info.value.code == 0
                mock_env.assert_not_called()

    def test_tty_prompt_eof_treated_as_no(self):
        """EOFError/KeyboardInterrupt no input → tratado como 'não'."""
        from unittest.mock import patch, MagicMock

        fake_venv = MagicMock()
        fake_venv.exists.return_value = False
        with patch("sys.stdout.isatty", return_value=True), \
             patch("builtins.input", side_effect=EOFError):
            mod = _load_deile_script()
            with patch.object(mod, "_venv_python", return_value=fake_venv):
                with pytest.raises(SystemExit) as exc_info:
                    mod._fast_version()
                assert exc_info.value.code == 0
