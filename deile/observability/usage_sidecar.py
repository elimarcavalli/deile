"""Usage sidecar — writes usage envelope to DEILE_USAGE_SIDECAR on oneshot exit.

Schema v1: {"schema_version": 1, "cost_usd": float>=0, "tokens_in": int>=0,
            "tokens_out": int>=0, "turns": int>=0}

Lifecycle (subprocess bridge pattern):
  1. Caller (OneshotSubprocessAgentBridge in deilebot) allocates a fresh path via
     tempfile.mkstemp, removes the empty placeholder, and exports it in
     DEILE_USAGE_SIDECAR before spawning the deile subprocess.
  2. This module (called from cli._run_oneshot) checks for DEILE_USAGE_SIDECAR
     and writes the envelope after the agent run.
  3. Caller reads the file (if it exists) and removes it in a finally block.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Optional

try:
    from deile.storage.usage_repository import get_usage_repository
except Exception:  # pragma: no cover — missing at import time in minimal envs
    get_usage_repository = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SIDECAR_ENV = "DEILE_USAGE_SIDECAR"


@dataclass
class UsageEnvelope:
    """Usage envelope — schema_version 1."""
    schema_version: int
    cost_usd: float
    tokens_in: int
    tokens_out: int
    turns: int

    def to_dict(self) -> dict:
        return asdict(self)


def get_sidecar_path() -> Optional[str]:
    """Return the DEILE_USAGE_SIDECAR env var value, or None if unset/empty."""
    return os.environ.get(SIDECAR_ENV) or None


def write_usage_sidecar(envelope: UsageEnvelope, path: str) -> None:
    """Write *envelope* as JSON to *path*. Best-effort — logs on error."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(envelope.to_dict(), fh)
    except Exception:
        logger.warning("usage sidecar write failed at %r", path, exc_info=True)


def collect_and_write_sidecar(session_id: str) -> None:
    """Aggregate usage from UsageRepository for *session_id* and write sidecar.

    No-op when DEILE_USAGE_SIDECAR is not set. Swallows all exceptions so the
    caller (cli._run_oneshot) never fails because of a missing sidecar write.
    """
    path = get_sidecar_path()
    if not path:
        return
    try:
        _repo_fn = get_usage_repository
        if _repo_fn is None:  # import failed at startup
            return
        records = _repo_fn().records_for_session(session_id)
        envelope = UsageEnvelope(
            schema_version=1,
            cost_usd=sum(r.cost_usd for r in records),
            tokens_in=sum(r.prompt_tokens for r in records),
            tokens_out=sum(r.completion_tokens for r in records),
            turns=len(records),
        )
        write_usage_sidecar(envelope, path)
    except Exception:
        logger.warning("collect_and_write_sidecar failed for session %r", session_id, exc_info=True)
