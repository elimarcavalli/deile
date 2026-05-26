"""Logger utilities for DEILE."""

import logging
import os
import sys
from pathlib import Path

_LOGGER_NAME = "deile"
_initialized = False
_encrypt_logs_warned = False


def _is_running_under_pytest() -> bool:
    """Detecta se estamos rodando dentro de pytest.

    Dois sinais redundantes:
    - ``"pytest" in sys.modules`` — verdadeiro durante toda a execução
      de uma sessão pytest (mesmo antes do primeiro teste). Cobre
      ``_ensure_initialized()`` chamado em import-time por um conftest.
    - ``PYTEST_CURRENT_TEST`` em env — setado pelo pytest entre o setUp
      e tearDown de cada teste; cobre código que importa ``deile``
      lazy dentro de um teste.

    Sem essa guarda, milhares de mensagens vindas de mocks/MagicMock
    (`'MagicMock' object can't be awaited`, etc.) vazam pro
    ``~/.deile/logs/deile.log`` real do operador e poluem o painel.
    """
    return ("pytest" in sys.modules
            or "PYTEST_CURRENT_TEST" in os.environ)


def _is_encrypt_logs_enabled() -> bool:
    """Return True when the active profile requests log encryption.

    Separated from _ensure_initialized so tests can patch this function
    without fighting lazy-import semantics.
    """
    try:
        from ..config.settings import get_settings  # noqa: PLC0415

        return bool(get_settings().encrypt_logs)
    except Exception:  # pragma: no cover — guard against import errors at startup
        return False


def _ensure_initialized() -> None:
    global _initialized, _encrypt_logs_warned
    if _initialized:
        return

    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        if _is_running_under_pytest():
            # Em pytest: NullHandler discarda tudo. Testes que precisam
            # inspecionar logs devem usar a fixture `caplog` do pytest.
            handler: logging.Handler = logging.NullHandler()
        else:
            log_dir = Path.home() / ".deile" / "logs"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                # Rotação custom: 1 arquivo por hora arquivado em subpasta
                # diária (`<logs>/<YYYY-MM-DD>/<HH>.log`), com `deile.log`
                # como o "current hour" no raiz pra compatibilidade com
                # leitores existentes (`tail -F`, painel TUI, etc.).
                # Retenção default 30 dias.
                from .log_rotation import \
                    HourlyDailyDirRotatingHandler  # noqa: PLC0415
                handler = HourlyDailyDirRotatingHandler(
                    filename=str(log_dir / "deile.log"),
                    encoding="utf-8",
                )
            except OSError:
                handler = logging.StreamHandler()
        # `[%(process)d]` indispensável quando múltiplos processos DEILE
        # escrevem no MESMO `~/.deile/logs/deile.log` (caso normal — o
        # FileHandler em append-mode é process-safe no POSIX porque cada
        # write() em fd com O_APPEND é atômico até PIPE_BUF=4096 bytes).
        # Sem o PID, atribuição de linha→processo no painel TUI ficava
        # impossível (motivou toda a arquitetura runtime/InstanceState).
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(process)d] [%(levelname)s] %(name)s: %(message)s"
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    _initialized = True

    # Warn once when encrypt_logs=True is set but not yet implemented (issue #138).
    if not _encrypt_logs_warned:
        _encrypt_logs_warned = True
        if _is_encrypt_logs_enabled():
            logging.getLogger(_LOGGER_NAME).warning(
                "encrypt_logs=True is set in the active profile, but log-file "
                "encryption is not yet implemented. Logs are written in plain text."
            )


def get_logger(name: str | None = None) -> logging.Logger:
    _ensure_initialized()
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)
