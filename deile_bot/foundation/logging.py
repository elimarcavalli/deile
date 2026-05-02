"""JSON-structured logging for the bot foundation (F9 of master plan)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

try:
    from pythonjsonlogger import jsonlogger
except ImportError:  # pragma: no cover
    jsonlogger = None  # type: ignore[assignment]


_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _build_json_formatter() -> logging.Formatter:
    if jsonlogger is None:  # pragma: no cover
        return logging.Formatter(_DEFAULT_FORMAT)
    return jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
    )


def setup_logging(
    log_dir: Optional[Path] = None,
    *,
    level: int = logging.INFO,
    file_name: str = "deile_bot.log",
) -> None:
    """Configure a JSON-structured stdout handler + RotatingFileHandler."""
    root = logging.getLogger("deile_bot")
    root.handlers.clear()
    root.setLevel(level)
    formatter = _build_json_formatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / file_name,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("deile_bot"):
        name = f"deile_bot.{name}"
    return logging.getLogger(name)
