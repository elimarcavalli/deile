"""Helpers compartilhados pelas ``cron_*`` tools.

As tools ``cron_create``/``cron_list``/``cron_delete`` repetiam o mesmo
bloco de tratamento de exceção inesperada em ``execute()``. Este módulo
centraliza esse padrão (DRY) — nenhuma das tools precisa de estado, então
o helper vive como função de módulo.
"""

from __future__ import annotations

from .base import ToolResult


def unexpected_error(exc: Exception) -> ToolResult:
    """Padroniza o ``ToolResult`` para uma exceção não-esperada de cron tool.

    Usa o nome da classe da exceção como prefixo da mensagem (em vez de
    vazar a string crua) e o ``error_code`` ``UNEXPECTED`` que os callers
    do agente já reconhecem.
    """
    return ToolResult.error_result(
        message=f"{type(exc).__name__}: {exc}",
        error=exc,
        error_code="UNEXPECTED",
    )
