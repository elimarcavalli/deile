"""Handler de log com cap por tamanho + rotação diária + dual-write.

Implementa :class:`CappedRotatingFileHandler`: um handler que escreve em
arquivo com limite de tamanho (MB) e número máximo de backups, faz rotação
forçada diária (meia-noite UTC), e espelha toda saída para stdout
(dual-write) — o K8s continua capturando logs via stdout para
compatibilidade com ferramentas existentes.

Uso típico::

    from deile.log_mgmt import init_logging
    init_logging(pod_name="deile-pipeline")
"""

from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


class CappedRotatingFileHandler(RotatingFileHandler):
    """Handler que escreve em arquivo E em stdout simultaneamente.

    Herda de :class:`RotatingFileHandler` para rotação por tamanho. Um timer
    periódico força rotação diária (meia-noite UTC). Dual-write: toda
    mensagem vai para o arquivo rotacionado E para ``sys.stdout``.

    Attributes:
        max_bytes: Tamanho máximo do arquivo em bytes antes de rotacionar.
        backup_count: Número de backups preservados.
        last_rotation_day: Dia do último rotation forçado (evita múltiplas
            rotações no mesmo dia).
    """

    def __init__(
        self,
        filename: str,
        max_mb: int = 5,
        backup_count: int = 3,
        encoding: str = "utf-8",
    ) -> None:
        max_bytes = max_mb * 1024 * 1024
        # Cria o diretório pai se necessário
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(
            filename=filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
        )
        self._stdout_handler: Optional[logging.StreamHandler] = None
        self.last_rotation_day: Optional[int] = None

    def set_stdout_handler(self, handler: logging.StreamHandler) -> None:
        """Registra um handler de stdout para dual-write."""
        self._stdout_handler = handler

    def emit(self, record: logging.LogRecord) -> None:
        """Escreve no arquivo rotacionado E em stdout (se configurado)."""
        # Rotação diária forçada (antes de emitir)
        self._maybe_force_daily_rotation()
        # Arquivo rotacionado
        super().emit(record)
        # stdout (dual-write)
        if self._stdout_handler is not None:
            self._stdout_handler.emit(record)

    def _maybe_force_daily_rotation(self) -> None:
        """Força rotação se o dia UTC mudou desde o último rotation."""
        now = time.time()
        current_day = time.gmtime(now).tm_yday
        if self.last_rotation_day is None:
            self.last_rotation_day = current_day
            return
        if current_day != self.last_rotation_day:
            self.doRollover()
            self.last_rotation_day = current_day

    def should_rollover(self, record: logging.LogRecord) -> bool:
        """Hook: também rotaciona se o tamanho excedeu maxBytes."""
        if self.stream is None:
            self.stream = self._open()
        if self.maxBytes > 0:
            msg = self.format(record) + self.terminator
            # Codifica para estimar bytes (aproximado mas suficiente)
            try:
                msg_bytes = len(msg.encode(self.encoding or "utf-8"))
            except Exception:
                msg_bytes = len(msg)
            self.stream.seek(0, 2)  # vai pro final
            if self.stream.tell() + msg_bytes >= self.maxBytes:
                return True
        return False


def _default_log_dir(pod_name: str) -> str:
    """Retorna o diretório de logs padrão para o pod.

    Respeita ``DEILE_LOG_DIR`` se definido; caso contrário usa
    ``/home/deile/logs/<pod_name>/``.
    """
    env_dir = os.environ.get("DEILE_LOG_DIR", "").strip()
    if env_dir:
        return env_dir
    home = os.environ.get("HOME", "/home/deile")
    return str(Path(home) / "logs" / pod_name)


def _default_log_file(pod_name: str) -> str:
    """Retorna o caminho completo do arquivo de log para o pod."""
    return str(Path(_default_log_dir(pod_name)) / f"{pod_name}.log")


def create_log_handler(
    pod_name: str,
    *,
    max_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
) -> CappedRotatingFileHandler:
    """Cria e configura um :class:`CappedRotatingFileHandler` para o pod.

    Args:
        pod_name: Nome do pod (ex: ``deile-pipeline``).
        max_mb: Tamanho máximo em MB (default: ``DEILE_LOG_MAX_SIZE_MB`` ou 5).
        backup_count: Número de backups (default: ``DEILE_LOG_BACKUP_COUNT`` ou 3).

    Returns:
        Handler configurado com formatter padrão e dual-write para stdout.
    """
    if max_mb is None:
        max_mb = int(os.environ.get("DEILE_LOG_MAX_SIZE_MB", "5"))
    if backup_count is None:
        backup_count = int(os.environ.get("DEILE_LOG_BACKUP_COUNT", "3"))

    log_file = _default_log_file(pod_name)
    handler = CappedRotatingFileHandler(
        filename=log_file,
        max_mb=max_mb,
        backup_count=backup_count,
    )

    # Formatter padrão (compatível com o formato atual dos pods)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    handler.setFormatter(formatter)

    # Dual-write: stdout handler com mesmo formatter
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    handler.set_stdout_handler(stdout_handler)

    return handler


def get_pod_name() -> str:
    """Heurística para descobrir o nome do pod.

    Tenta ``DEILE_POD_NAME`` env var; fallback para ``HOSTNAME`` (K8s
    define como nome do pod); fallback final ``unknown``.
    """
    for key in ("DEILE_POD_NAME", "HOSTNAME"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return "unknown"
