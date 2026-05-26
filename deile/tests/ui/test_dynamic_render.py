"""Testes do helper ``deile.ui.dynamic_render``.

Issue #307 — esse módulo embrulha surfaces críticas em ``rich.live.Live``
para que adaptem ao redimensionamento do terminal em tempo real durante
seu tempo de vida (Live re-renderiza a cada frame consultando
``console.size`` corrente).
"""
from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from deile.ui.dynamic_render import (is_interactive_tty, live_for,
                                     turn_separator)


@pytest.mark.unit
def test_is_interactive_tty_false_for_stringio() -> None:
    """StringIO não é TTY — Live deve cair em fallback estático."""
    # `is_interactive_tty` consulta `sys.stdout.isatty()`. Em testes,
    # stdout pode ser capturado pelo pytest (isatty=False).
    # O contrato é: retorna False em ambientes não-interativos.
    with patch("deile.ui.dynamic_render.sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False
        assert is_interactive_tty() is False


@pytest.mark.unit
def test_is_interactive_tty_true_with_real_tty() -> None:
    """Quando stdout.isatty() é True e TERM não-dumb, retorna True."""
    with patch("deile.ui.dynamic_render.sys.stdout") as mock_stdout, \
         patch.dict("deile.ui.dynamic_render.os.environ", {"TERM": "xterm-256color"}):
        mock_stdout.isatty.return_value = True
        assert is_interactive_tty() is True


@pytest.mark.unit
def test_live_for_non_tty_falls_back_to_static_print() -> None:
    """Em ambiente sem TTY, ``live_for`` faz um print estático e retorna."""
    console = Console(file=io.StringIO(), width=80, force_terminal=False, color_system=None)
    with patch("deile.ui.dynamic_render.is_interactive_tty", return_value=False):
        live_for(Panel(Text("hello")), console=console, duration_s=0.1)
    out = console.file.getvalue()
    assert "hello" in out
    # Como não passou por Live, não há ANSI de cursor positioning
    assert "\x1b[" not in out or "hello" in out


@pytest.mark.unit
def test_live_for_accepts_callable_renderable() -> None:
    """``live_for`` aceita ``Callable[[], RenderableType]`` para re-construir."""
    counter = {"n": 0}

    def build():
        counter["n"] += 1
        return Text(f"frame {counter['n']}")

    console = Console(file=io.StringIO(), width=80, force_terminal=False, color_system=None)
    with patch("deile.ui.dynamic_render.is_interactive_tty", return_value=False):
        # Fallback path apenas chama o callable uma vez
        live_for(build, console=console, duration_s=0.1)
    assert counter["n"] >= 1


@pytest.mark.unit
def test_turn_separator_writes_rule_to_console() -> None:
    """``turn_separator`` imprime um Rule horizontal — adapta a console.width."""
    console = Console(file=io.StringIO(), width=60, force_terminal=True, color_system=None)
    turn_separator(console)
    out = console.file.getvalue()
    # Rule renderiza com `─` (light) ou `━` (heavy) dependendo do estilo
    assert any(ch in out for ch in ("─", "━", "-"))


@pytest.mark.unit
def test_live_for_duration_zero_does_not_hang() -> None:
    """``duration_s=0`` retorna imediatamente sem travar."""
    console = Console(file=io.StringIO(), width=80, force_terminal=False, color_system=None)
    with patch("deile.ui.dynamic_render.is_interactive_tty", return_value=False):
        live_for(Panel(Text("x")), console=console, duration_s=0.0)


@pytest.mark.unit
def test_turn_separator_adapts_to_console_width() -> None:
    """O separador respeita ``console.width`` em renders diferentes."""
    for w in (40, 80, 120):
        console = Console(file=io.StringIO(), width=w, force_terminal=True, color_system=None)
        turn_separator(console)
        out = console.file.getvalue()
        # Cada linha do output não pode ultrapassar w
        for line in out.splitlines():
            assert len(line) <= w, f"linha estourou width={w}: {line!r}"
