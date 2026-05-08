"""Logger utilities for DEILE."""

import logging
from pathlib import Path

_LOGGER_NAME = "deile"
_initialized = False


def _ensure_initialized() -> None:
    global _initialized
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


def get_logger(name: str | None = None) -> logging.Logger:
    _ensure_initialized()
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)
