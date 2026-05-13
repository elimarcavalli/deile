"""SessionHistoryStore — persists conversation history for /resume."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SESSIONS_DIR = Path.home() / ".deile" / "sessions"


class SessionHistoryStore:
    """Saves and lists conversation histories for the /resume command.

    Each session is stored as ``~/.deile/sessions/<session_id>/history.json``
    so that /resume can restore past conversations across process restarts.
    Writes are atomic (write to .tmp, then rename).
    """

    def __init__(self, base_dir: Path = _SESSIONS_DIR) -> None:
        self._base_dir = base_dir

    def save(self, session_id: str, history: List[Dict], name: str = "") -> None:
        """Persist the current conversation_history to disk (best-effort)."""
        if not history:
            return
        session_dir = self._base_dir / session_id
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "session_id": session_id,
                "conversation_name": name,
                "last_activity": time.time(),
                "history": history,
            }
            tmp = session_dir / "history.tmp"
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(session_dir / "history.json")
        except Exception:
            pass  # non-fatal

    def list_sessions(self, max_sessions: int = 50) -> List[Dict[str, Any]]:
        """Return sessions sorted by last_activity descending."""
        if not self._base_dir.exists():
            return []
        sessions: List[Dict[str, Any]] = []
        for session_dir in self._base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            history_file = session_dir / "history.json"
            if not history_file.exists():
                continue
            try:
                data = json.loads(history_file.read_text(encoding="utf-8"))
                user_msgs = [
                    m for m in data.get("history", [])
                    if m.get("role") == "user" and not m.get("content", "").startswith("/")
                ]
                sessions.append({
                    "session_id": data["session_id"],
                    "conversation_name": data.get("conversation_name", ""),
                    "last_activity": float(data.get("last_activity", 0.0)),
                    "first_user_input": user_msgs[0]["content"] if user_msgs else "",
                    "message_count": len(data.get("history", [])),
                })
            except Exception:
                continue
        sessions.sort(key=lambda s: s["last_activity"], reverse=True)
        return sessions[:max_sessions]

    def load(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the stored session data, or None if missing/corrupt."""
        history_file = self._base_dir / session_id / "history.json"
        if not history_file.exists():
            return None
        try:
            return json.loads(history_file.read_text(encoding="utf-8"))
        except Exception:
            return None
