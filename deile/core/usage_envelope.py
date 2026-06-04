from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from deile.storage.usage_repository import get_usage_repository

logger = logging.getLogger(__name__)


def _get_usage_records(session_id: str) -> list:
    """Return usage records for *session_id* from the repository.

    Extracted as a module-level helper so tests can monkeypatch it without
    touching the singleton UsageRepository.
    """
    return get_usage_repository().records_for_session(session_id)


def build_usage_envelope(session_id: str) -> dict:
    """Build a usage envelope dict from UsageRepository records for the given session.

    Returns a dict with keys:
        schema_version: int (always 1)
        cost_usd: float
        tokens_in: int
        tokens_out: int
        turns: int
    """
    records = _get_usage_records(session_id)

    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    turns: int = len(records)

    for r in records:
        # Support both dataclass attrs and plain dict (for testability)
        if isinstance(r, dict):
            cost_usd += float(r.get("cost_usd", 0.0) or 0.0)
            tokens_in += int(r.get("prompt_tokens", 0) or 0)
            tokens_out += int(r.get("completion_tokens", 0) or 0)
        else:
            cost_usd += float(getattr(r, "cost_usd", 0.0) or 0.0)
            tokens_in += int(getattr(r, "prompt_tokens", 0) or 0)
            tokens_out += int(getattr(r, "completion_tokens", 0) or 0)

    return {
        "schema_version": 1,
        "cost_usd": cost_usd,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "turns": turns,
    }


def write_usage_sidecar(session_id: str) -> None:
    """Write a usage sidecar JSON file for the given session.

    Reads DEILE_USAGE_SIDECAR from os.environ. If not set, returns immediately
    (noop). Builds the usage envelope and writes JSON to that path atomically
    (write to .tmp then rename). On any error, logs a warning but never raises.
    """
    sidecar_path_str = os.environ.get("DEILE_USAGE_SIDECAR")
    if not sidecar_path_str:
        return

    try:
        envelope = build_usage_envelope(session_id)
        sidecar_path = Path(sidecar_path_str)
        sidecar_dir = sidecar_path.parent
        sidecar_dir.mkdir(parents=True, exist_ok=True)

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=sidecar_dir,
            prefix=".usage_sidecar_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(envelope, f)
            Path(tmp_path).rename(sidecar_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning(
            "write_usage_sidecar: failed to write sidecar for session %s: %s",
            session_id,
            exc,
        )
