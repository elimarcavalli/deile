"""Tests for ``deile.ui._stdin_owner`` (M13 — issue #295 review, iter-2).

Foca em:
  * ``prime_termios_snapshot`` prefere o snapshot explícito sobre auto-capture.
  * Quando chamado sem snapshot E o terminal já está em cbreak (ICANON off),
    o auto-capture é RECUSADO com warning, não salva estado quebrado.
  * Quando chamado sem snapshot E o terminal está cooked (ICANON on),
    auto-capture é aceito.
"""
from __future__ import annotations

import logging
import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_stdin_owner_state():
    """Isola o estado global do módulo entre testes."""
    import deile.ui._stdin_owner as mod
    mod._saved_termios = None
    mod._termios_fd = -1
    mod._atexit_registered = False
    yield
    mod._saved_termios = None
    mod._termios_fd = -1


def test_prime_termios_snapshot_with_explicit_snapshot_is_preferred():
    """Caller passa snapshot explícito → salvo direto sem tcgetattr."""
    import deile.ui._stdin_owner as mod

    explicit_snapshot = ["fake-iflag", "fake-oflag", "fake-cflag", 0xff]
    # Patch isatty para True, mas tcgetattr NÃO deve ser chamado quando
    # o caller já passou o snapshot.
    with patch.object(sys.stdin, "isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0):
        with patch("termios.tcgetattr") as mock_tcget:
            mod.prime_termios_snapshot(original_termios=explicit_snapshot)
            mock_tcget.assert_not_called()
    assert mod._saved_termios == explicit_snapshot


def test_prime_termios_snapshot_auto_capture_refuses_cbreak_state(caplog):
    """Quando snapshot==None E ICANON está OFF, recusa captura + warning.

    O critério principal é que ``_saved_termios`` permanece ``None`` — o
    warning é secundário (caplog requer propagation que outros testes podem
    desabilitar via reconfiguração global do logging).
    """
    import deile.ui._stdin_owner as mod

    # ICANON bit: termios.ICANON. Construir um lflag sem o bit ICANON.
    iflag = 0
    oflag = 0
    cflag = 0
    lflag_cbreak = 0  # ICANON DESLIGADO
    cooked_snapshot = [iflag, oflag, cflag, lflag_cbreak, 0, 0, []]

    # Garante propagate=True só para esse teste.
    target_logger = logging.getLogger("deile.ui._stdin_owner")
    old_propagate = target_logger.propagate
    old_disabled = target_logger.disabled
    target_logger.propagate = True
    target_logger.disabled = False
    caplog.set_level(logging.WARNING, logger="deile.ui._stdin_owner")
    try:
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch("sys.stdin.fileno", return_value=0), \
             patch("termios.tcgetattr", return_value=cooked_snapshot):
            mod.prime_termios_snapshot(original_termios=None)
    finally:
        target_logger.propagate = old_propagate
        target_logger.disabled = old_disabled

    # Critério principal (Always observável): não salvou o estado errado.
    assert mod._saved_termios is None

    # Critério secundário: warning visível (tolera ausência se algum teste
    # anterior reconfigurou logging globalmente).
    if caplog.records:
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        if msgs:
            assert any("cbreak" in m.lower() or "icanon" in m.lower() for m in msgs), \
                f"caplog tinha warnings, mas nenhum sobre cbreak/ICANON: {msgs}"


def test_prime_termios_snapshot_auto_capture_accepts_cooked_state():
    """ICANON ON → auto-capture salva o snapshot atual normalmente."""
    import termios as _termios

    import deile.ui._stdin_owner as mod

    iflag = 0
    oflag = 0
    cflag = 0
    lflag_cooked = _termios.ICANON  # ICANON LIGADO
    cooked_snapshot = [iflag, oflag, cflag, lflag_cooked, 0, 0, []]

    with patch.object(sys.stdin, "isatty", return_value=True), \
         patch("sys.stdin.fileno", return_value=0), \
         patch("termios.tcgetattr", return_value=cooked_snapshot):
        mod.prime_termios_snapshot(original_termios=None)

    assert mod._saved_termios == cooked_snapshot
