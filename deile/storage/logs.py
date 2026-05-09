"""Logger utilities for DEILE."""

import logging
from pathlib import Path

_LOGGER_NAME = "deile"
_initialized = False
_encrypt_logs_warned = False


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
        log_dir = Path.home() / ".deile" / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_dir / "deile.log", encoding="utf-8")
        except OSError:
            handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
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
