"""ConversationNameStore — lightweight JSON persistence for conversation names.

Stored at ``~/.deile/conversation_names.json``.  Resilient to missing file
and concurrent writes (last-write-wins; no lock needed for this use-case).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STORE_PATH = Path.home() / ".deile" / "conversation_names.json"


class ConversationNameStore:
    """Maps session_id → human-readable conversation name."""

    def __init__(self, path: Path = _STORE_PATH) -> None:
        self._path = path

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            logger.debug("conversation_names.json unreadable — starting fresh")
            return {}

    def _save(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not persist conversation name: %s", exc)

    def get(self, session_id: str) -> Optional[str]:
        return self._load().get(session_id)

    def set(self, session_id: str, name: str) -> None:
        data = self._load()
        data[session_id] = name
        self._save(data)

    def delete(self, session_id: str) -> None:
        data = self._load()
        data.pop(session_id, None)
        self._save(data)

    def all(self) -> dict:
        return self._load()
