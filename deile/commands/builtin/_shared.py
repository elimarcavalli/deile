"""Helpers compartilhados pelos comandos builtin.

Centraliza padrões repetidos (parsing de args, painéis Rich, mapas
PT-BR de descrições, recuperação de subsistemas via `context.agent`)
que apareciam duplicados em vários `*_command.py`.
"""

from __future__ import annotations

from typing import Any, List

from ..base import CommandContext


def split_args(context: CommandContext) -> List[str]:
    """Tokeniza `context.args` em uma lista de palavras.

    Trata `args` ausente, vazio ou só com espaços como `[]`. Substitui
    o idioma `args = context.args if hasattr(context, "args") else ""`
    seguido de `parts = args.strip().split() if args.strip() else []`.
    """
    raw = getattr(context, "args", "") or ""
    stripped = raw.strip()
    return stripped.split() if stripped else []
