"""Regressão: Enter sem texto no prompt interativo NÃO deve empilhar
``>`` (prompt commitado) e ``─`` (rules de separação) na tela.

Bug pós-PR #312: o cleanup antigo emitia apenas ``\\033[A\\033[2K\\r``
(uma única iteração de "up + erase-line"), o que apagava só a linha do
prompt — mas ``get_user_input`` emite 2 linhas por iteração:
``console.rule(style="dim")`` (separador horizontal) + a linha ``> ``
que o prompt_toolkit commita ao receber Enter. Resultado: rules
empilhados (e prompts em terminais cujo emulador não consolida o
prompt-area). Fix: apagar 2 linhas via ``\\033[A\\033[2K`` duas vezes.

Esses testes cobrem o helper ``_DeileCLI._erase_empty_prompt_echo``
sem precisar de TTY real nem de inicialização completa do CLI (que
requer API keys).
"""
from __future__ import annotations

import io
import sys
from unittest.mock import patch

import pytest

from deile.cli import _DeileCLI


@pytest.mark.unit
def test_erase_empty_prompt_echo_writes_two_line_cleanup_when_tty() -> None:
    """Quando stdout é TTY, escreve 2 ANSI ``up + erase`` em sequência.

    O contrato é APAGAR 2 LINHAS (rule + prompt), não 1. Antes do fix,
    o cleanup era ``\\033[A\\033[2K\\r`` (1 linha apenas) — deixava o
    rule emitido por ``console.rule(style="dim")`` no scrollback.
    """
    cli = _DeileCLI.__new__(_DeileCLI)
    buf = io.StringIO()

    class _TTYBuf(io.StringIO):
        def isatty(self) -> bool:  # noqa: D401
            return True

    fake_stdout = _TTYBuf()
    with patch.object(sys, "stdout", fake_stdout):
        cli._erase_empty_prompt_echo()
    written = fake_stdout.getvalue()

    # Deve conter o cursor-up + erase-line REPETIDOS (2x).
    up_count = written.count("\033[A")
    assert up_count == 2, (
        f"deveria ter 2 cursor-up; got {up_count}: {written!r}"
    )
    erase_count = written.count("\033[2K")
    assert erase_count == 2, (
        f"deveria ter 2 erase-line; got {erase_count}: {written!r}"
    )
    # E reposicionar ao início (carriage return).
    assert written.endswith("\r"), f"deveria terminar em \\r: {written!r}"


@pytest.mark.unit
def test_erase_empty_prompt_echo_writes_nothing_when_not_tty() -> None:
    """Sem TTY (pipe, CI, redirect), NÃO emite ANSI no output.

    Caso contrário, ``\\033[A`` literal vazaria como bytes no pipe e
    poluiria logs, arquivos, output de CI etc.
    """
    cli = _DeileCLI.__new__(_DeileCLI)

    class _NotTTYBuf(io.StringIO):
        def isatty(self) -> bool:  # noqa: D401
            return False

    fake_stdout = _NotTTYBuf()
    with patch.object(sys, "stdout", fake_stdout):
        cli._erase_empty_prompt_echo()
    assert fake_stdout.getvalue() == "", (
        f"deveria ser silencioso sem TTY: {fake_stdout.getvalue()!r}"
    )


@pytest.mark.unit
def test_erase_ansi_constant_pair_count_matches_lines_emitted_per_prompt() -> None:
    """A constante ``_ERASE_PROMPT_ECHO_ANSI`` espelha o número de
    linhas emitidas por iteração de ``get_user_input``.

    ``get_user_input`` emite:
      1. ``self.console.rule(style="dim")`` → 1 linha
      2. prompt_toolkit commita ``> `` → 1 linha
    Total = 2 linhas. Apagamos 2 (pair ``\\033[A\\033[2K`` aparece 2x).

    Esse teste prende o invariante: se algum dia ``get_user_input``
    passar a emitir mais (ou menos) linhas, esse contador precisa
    casar — daí a inflação de prompts em branco que motivou a fix.
    """
    ansi = _DeileCLI._ERASE_PROMPT_ECHO_ANSI
    assert ansi.count("\033[A") == 2
    assert ansi.count("\033[2K") == 2
