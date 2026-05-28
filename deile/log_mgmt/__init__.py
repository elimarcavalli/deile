"""DEILE Logger — gestão de logs com rotação, análise de anomalias e dispatch.

Uso nos entrypoints::

    from deile.log_mgmt import init_logging
    init_logging(pod_name="deile-pipeline")

Isto substitui o ``logging.basicConfig(...)`` padrão por um
:class:`CappedRotatingFileHandler` que escreve em arquivo E em stdout
(dual-write), com limpeza automática e rotação por tamanho/diária.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional, TYPE_CHECKING

from deile.log_mgmt.log_rotator import create_log_handler, get_pod_name

if TYPE_CHECKING:
    from deile.log_mgmt.log_rotator import CappedRotatingFileHandler


def init_logging(
    pod_name: Optional[str] = None,
    *,
    level: Optional[str] = None,
    max_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
) -> "CappedRotatingFileHandler":
    """Inicializa o sistema de logging do DEILE.

    Substitui ``logging.basicConfig()``. Configura o root logger com
    um :class:`CappedRotatingFileHandler` que faz dual-write (arquivo +
    stdout) e rotação automática.

    Args:
        pod_name: Nome do pod. Se None, tenta detectar via env var
            ``DEILE_POD_NAME`` ou ``HOSTNAME``.
        level: Nível de log (ex: ``INFO``, ``DEBUG``). Default via env
            ``DEILE_LOG_LEVEL`` ou ``INFO``.
        max_mb: Tamanho máximo do arquivo em MB. Default via env
            ``DEILE_LOG_MAX_SIZE_MB`` ou 5.
        backup_count: Número de backups. Default via env
            ``DEILE_LOG_BACKUP_COUNT`` ou 3.

    Returns:
        O handler configurado.
    """
    import os

    if pod_name is None:
        pod_name = get_pod_name()

    if level is None:
        level = os.environ.get("DEILE_LOG_LEVEL", "INFO")

    # Resolve nível
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Cria handler
    handler = create_log_handler(
        pod_name=pod_name,
        max_mb=max_mb,
        backup_count=backup_count,
    )

    # Configura root logger
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove handlers existentes para evitar duplicação
    root.handlers.clear()

    # Adiciona nosso handler
    root.addHandler(handler)

    # Log de inicialização
    root.info(
        "DEILE Logger initialized: pod=%s level=%s max_mb=%s backups=%s",
        pod_name,
        level,
        max_mb if max_mb is not None else os.environ.get("DEILE_LOG_MAX_SIZE_MB", "5"),
        backup_count if backup_count is not None else os.environ.get("DEILE_LOG_BACKUP_COUNT", "3"),
    )

    return handler
