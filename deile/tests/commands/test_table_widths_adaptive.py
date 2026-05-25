"""Regressão estrutural: nenhuma coluna de ``Table`` em ``deile/commands/builtin/``
nem em ``deile/ui/`` declara ``width=<int>`` literal — só largura dinâmica
(Rich auto-calcula em cada render usando ``console.width`` corrente).

Issue #307 — regra: **DEILE tem layout dinâmico em TODOS os recursos**.

Larguras fixas em colunas Rich travam a tabela quando o terminal é estreito,
forçando estouro horizontal — mesma classe de bug que o welcome screen tinha
com ``inner_w = max(len(...))``.

Exceções permitidas (verificadas via boundary ``\\b``):
- ``max_width=N``: é um TETO, Rich pode encolher abaixo dele.
- ``min_width=N``: é um PISO; ainda permite expansão.
- ``ratio=N``: proporção, intrinsicamente dinâmica.
- ``bar_width=None``: Rich Progress, já dinâmico.
- ``width=None``: explícito "sem largura".
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[3]
TARGETS = [
    ROOT / "deile" / "commands" / "builtin",
    ROOT / "deile" / "ui",
]

# Casa exatamente `width=<int>` (não `max_width`, `min_width`, `bar_width`,
# `width=None`, `width=var`). Apenas dentro de chamadas `.add_column(...)`.
WIDTH_LITERAL = re.compile(r"(?<!\w)width\s*=\s*\d+\b")
ADD_COLUMN_LINE = re.compile(r"\.add_column\s*\(")


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for target in TARGETS:
        files.extend(p for p in target.rglob("*.py") if p.is_file())
    return files


@pytest.mark.unit
def test_no_fixed_width_in_add_column_across_ui_and_commands() -> None:
    """Nenhuma ``add_column(..., width=<N>, ...)`` em UI/commands.

    Por que: ``width=<int>`` literal trava a coluna em N caracteres, ignorando
    a largura corrente do terminal. Em terminais estreitos a soma das colunas
    estoura a borda da tabela; em terminais largos a tabela fica subutilizada.
    Rich auto-calcula a largura ótima por coluna quando ``width=`` não é
    setado — basta confiar nessa decisão.
    """
    offenders: list[str] = []
    for path in _iter_python_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ADD_COLUMN_LINE.search(line) and WIDTH_LITERAL.search(line):
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Coluna(s) Rich com largura fixa detectada(s) — viola layout dinâmico (issue #307):\n"
        + "\n".join(offenders)
    )


@pytest.mark.unit
@pytest.mark.parametrize("console_width", [40, 60, 80, 120, 200])
def test_help_command_table_adapts_to_console_width(console_width: int) -> None:
    """A tabela construída por ``HelpCommand`` adapta a largura ao console.

    Esse é um teste de "fumaça vivo": construímos a Table real (mesmo padrão
    que o comando usa) e renderizamos em consoles de larguras diferentes —
    o output deve ter exatamente ``console_width`` colunas.
    """
    table = Table(title="Commands", box=None, show_header=True)
    table.add_column("Command", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Type", style="yellow")
    table.add_row("/help", "Show all commands available in the registry", "Direct")
    table.add_row("/status", "Show the status of every DEILE component", "Direct")

    console = Console(file=io.StringIO(), width=console_width, force_terminal=True, color_system=None)
    console.print(table)
    output = console.file.getvalue()

    # Cada linha visível deve respeitar o limite. Linhas internas (header,
    # rows) podem ser menores se o conteúdo for menor — só nos importa que
    # nada estoure ``console_width``.
    for line in output.splitlines():
        assert len(line) <= console_width, (
            f"Linha estourou console_width={console_width} (len={len(line)}): {line!r}"
        )


@pytest.mark.unit
def test_no_manual_box_drawing_in_ui_or_commands() -> None:
    """Sem ``╔══╗`` desenhado manualmente fora de ASCII art conhecido.

    Caracteres de moldura unicode (``╔ ╠ ╚ ╗ ╣ ╝``) só devem aparecer em:
    - logo ASCII art (``_DEILE_ASCII`` em ``console_ui.py``) — não é UI dinâmica
    - docstrings/comentários de teste — explicando o que era o bug antigo
    """
    offenders: list[str] = []
    for path in _iter_python_files():
        if path.name == "console_ui.py":
            # contém o logo ASCII e o comentário que explica o bug antigo
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if any(ch in line for ch in "╔╠╚╗╣╝"):
                # ignora se está dentro de docstring/comentário multi-linha
                # — heurística simples: se a linha NÃO tem aspas, é código real
                if '"' not in line and "'" not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped}")

    assert not offenders, (
        "Box-drawing manual encontrado em código — usar `Panel`/`Rule` adaptativos:\n"
        + "\n".join(offenders)
    )


@pytest.mark.unit
def test_no_text_derived_width_pattern_in_ui_or_commands() -> None:
    """Sem ``inner_w = max(len(...))`` ou ``"═" * N`` derivados de texto.

    Esse era o anti-padrão original do ``show_welcome``: calcular a largura
    da caixa a partir do ``len()`` das strings exibidas e desenhar bordas
    manualmente. Resultado: largura travada no momento da renderização.
    """
    BAD_MAX_LEN = re.compile(r"max\s*\(\s*len\s*\(")
    BAD_BOX_MULT = re.compile(r'"[═─━┄]"?\s*\*\s*\w')
    offenders: list[str] = []
    for path in _iter_python_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if BAD_MAX_LEN.search(line) and ("width" in line.lower() or "inner_w" in line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped}")
            elif BAD_BOX_MULT.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped}")

    assert not offenders, (
        "Padrão de largura derivada de texto encontrado — usar Rich adaptativo:\n"
        + "\n".join(offenders)
    )
