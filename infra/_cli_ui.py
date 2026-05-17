"""Helpers de UI de terminal compartilhados pelos scripts de infra.

Apenas stdlib — o ``setup_environment.py`` importa este módulo ANTES de
qualquer ``pip install``, então ele não pode depender de pacote externo.

Todo texto voltado ao operador nos scripts de infra (`setup_environment.py`,
`deploy.py`) passa por aqui, para o visual ficar consistente — colorido,
em PT-BR, com símbolos de status.

As cores respeitam a convenção ``NO_COLOR`` e se desligam sozinhas quando
a saída não é um terminal. No Windows, o modo VT (sequências ANSI) é
habilitado em tempo de import.
"""

from __future__ import annotations

import getpass
import os
import sys
from typing import Optional, Sequence, Tuple

# Habilita o processamento de sequências ANSI no console do Windows 10+.
if sys.platform == "win32":  # pragma: no cover - específico de Windows
    try:
        import ctypes

        _kernel32 = ctypes.windll.kernel32
        # -11 = STD_OUTPUT_HANDLE; 7 = PROCESSED_OUTPUT | WRAP_AT_EOL |
        # VIRTUAL_TERMINAL_PROCESSING.
        _kernel32.SetConsoleMode(_kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

# stdout em line-buffering: cada print() é descarregado na hora, então a
# ordem fica correta quando a saída se intercala com a de subprocessos
# (kubectl, systemctl, etc.).
try:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

_COLOR_ENABLED = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("TERM") != "dumb"
)

_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}


def set_color(enabled: bool) -> None:
    """Força as cores ligadas/desligadas (usado por --no-color e testes)."""
    global _COLOR_ENABLED
    _COLOR_ENABLED = enabled


def color_enabled() -> bool:
    return _COLOR_ENABLED


def paint(text: str, *styles: str) -> str:
    """Envolve ``text`` com os estilos ANSI dados, se as cores estão ligadas."""
    if not _COLOR_ENABLED or not styles:
        return text
    prefix = "".join(_CODES.get(s, "") for s in styles)
    return f"{prefix}{text}{_CODES['reset']}"


# ----- blocos estruturais -----

def header(title: str) -> None:
    """Imprime um título em destaque, dentro de uma moldura."""
    line = "═" * (len(title) + 4)
    print()
    print(paint(f"╔{line}╗", "cyan", "bold"))
    print(paint(f"║  {title}  ║", "cyan", "bold"))
    print(paint(f"╚{line}╝", "cyan", "bold"))


def section(title: str) -> None:
    """Imprime um cabeçalho de seção."""
    fill = "─" * max(3, 56 - len(title))
    print()
    print(paint(f"── {title} ", "blue", "bold") + paint(fill, "blue"))


# ----- linhas de status -----

def ok(msg: str) -> None:
    print(paint("  ✓ ", "green", "bold") + msg)


def warn(msg: str) -> None:
    print(paint("  ⚠ ", "yellow", "bold") + msg)


def err(msg: str) -> None:
    sys.stdout.flush()  # mantém a ordem quando stdout é redirecionado
    print(paint("  ✗ ", "red", "bold") + msg, file=sys.stderr)


def info(msg: str) -> None:
    print(paint("  · ", "gray") + msg)


def detail(msg: str) -> None:
    print(paint("      " + msg, "gray"))


def step(n: int, total: int, msg: str) -> None:
    print(paint(f"  [{n}/{total}] ", "magenta", "bold") + paint(msg, "bold"))


def plain(msg: str = "") -> None:
    print(msg)


def command(label: str, value: str) -> None:
    """Imprime um comando sugerido, destacado."""
    print("      " + paint(label, "cyan", "bold") + paint(value, "cyan"))


# ----- prompts -----

def ask(question: str, default: Optional[str] = None) -> str:
    """Pergunta de texto. Vazio devolve o default; sem default, repete."""
    suffix = paint(f" [{default}]", "gray") if default else ""
    while True:
        ans = input(paint("  ? ", "cyan", "bold") + question + suffix + ": ").strip()
        if ans:
            return ans
        if default is not None:
            return default
        err("Resposta obrigatória.")


def ask_secret(question: str) -> str:
    """Pergunta um valor sensível — não ecoa no terminal."""
    while True:
        ans = getpass.getpass(
            paint("  ? ", "cyan", "bold") + question + ": "
        ).strip()
        if ans:
            return ans
        err("Resposta obrigatória.")


def confirm(question: str, default: bool = True) -> bool:
    """Pergunta sim/não. Enter vazio devolve o default."""
    hint = "[S/n]" if default else "[s/N]"
    ans = input(
        paint("  ? ", "cyan", "bold") + question + " " + paint(hint, "gray") + " "
    ).strip().lower()
    if not ans:
        return default
    return ans in ("s", "sim", "y", "yes")


def choose(question: str, options: Sequence[Tuple[str, str]]) -> str:
    """Menu numerado. ``options`` é uma lista de (chave, descrição).

    Devolve a chave escolhida. Aceita o número ou a própria chave.
    """
    for i, (key, desc) in enumerate(options, 1):
        print(
            "    "
            + paint(f"{i}) ", "magenta", "bold")
            + paint(key, "bold")
            + paint(f"  {desc}", "gray")
        )
    keys = [k for k, _ in options]
    while True:
        raw = input(
            paint("  ? ", "cyan", "bold") + question + f" [1-{len(keys)}]: "
        ).strip().lower()
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        if raw in keys:
            return raw
        err("Opção inválida.")


# ----- tabela de comandos (para o help) -----

def command_table(rows: Sequence[Tuple[str, str]]) -> None:
    """Imprime pares (comando, descrição) alinhados em duas colunas."""
    width = max((len(name) for name, _ in rows), default=0)
    for name, desc in rows:
        print("  " + paint(name.ljust(width), "green", "bold") + "  " + desc)
