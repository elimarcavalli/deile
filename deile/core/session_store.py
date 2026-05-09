"""Persistent session store for DeileAgent.

Backed by SQLite (default `./data/deile_sessions.sqlite`). Used by
`DeileAgent.get_or_create_session(session_id, *, persisted=True)` to
resurrect bot sessions across restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS persisted_session (
    session_id        TEXT PRIMARY KEY,
    working_directory TEXT NOT NULL,
    context_data_json TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    last_used_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_last_used ON persisted_session(last_used_at);
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class PersistedSessionRow:
    session_id: str
    working_directory: str
    context_data: Dict[str, Any]
    created_at: str
    last_used_at: str


class SessionStore:
    """Async SQLite store for DeileAgent persisted sessions."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        if self._db is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _require(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SessionStore not initialized; call init()")
        return self._db

    async def get(self, session_id: str) -> Optional[PersistedSessionRow]:
        db = self._require()
        cur = await db.execute(
            "SELECT session_id, working_directory, context_data_json, created_at, last_used_at "
            "FROM persisted_session WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        try:
            ctx = json.loads(row[2])
        except Exception:
            ctx = {}
        return PersistedSessionRow(
            session_id=row[0],
            working_directory=row[1],
            context_data=ctx,
            created_at=row[3],
            last_used_at=row[4],
        )

    async def upsert(
        self,
        session_id: str,
        working_directory: str,
        context_data: Dict[str, Any],
    ) -> None:
        async with self._lock:
            db = self._require()
            payload = self._safe_serialize(context_data)
            now = _utc_iso()
            await db.execute(
                """
                INSERT INTO persisted_session(
                    session_id, working_directory, context_data_json, created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    working_directory = excluded.working_directory,
                    context_data_json = excluded.context_data_json,
                    last_used_at = excluded.last_used_at
                """,
                (session_id, working_directory, payload, now, now),
            )
            await db.commit()

    async def touch(self, session_id: str) -> None:
        async with self._lock:
            db = self._require()
            await db.execute(
                "UPDATE persisted_session SET last_used_at = ? WHERE session_id = ?",
                (_utc_iso(), session_id),
            )
            await db.commit()

    async def purge_older_than(self, days: int) -> int:
        async with self._lock:
            db = self._require()
            cutoff = (datetime.now(timezone.utc).timestamp() - days * 86400)
            cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
            cur = await db.execute(
                "DELETE FROM persisted_session WHERE last_used_at < ?",
                (cutoff_iso,),
            )
            removed = cur.rowcount or 0
            await cur.close()
            await db.commit()
            return removed

    async def list_all(self) -> list:
        db = self._require()
        cur = await db.execute(
            "SELECT session_id, last_used_at FROM persisted_session ORDER BY last_used_at DESC"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [{"session_id": r[0], "last_used_at": r[1]} for r in rows]

    async def get_stats(self) -> dict:
        """Returns session count, oldest and newest last_used_at timestamps."""
        db = self._require()
        cur = await db.execute(
            "SELECT COUNT(*), MIN(last_used_at), MAX(last_used_at) FROM persisted_session"
        )
        row = await cur.fetchone()
        await cur.close()
        return {
            "session_count": row[0] or 0,
            "oldest_last_used": row[1],
            "newest_last_used": row[2],
        }

    async def count_sessions_before(self, cutoff_date: datetime) -> int:
        """Count sessions with last_used_at before cutoff_date (without deleting)."""
        db = self._require()
        cutoff_iso = cutoff_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cur = await db.execute(
            "SELECT COUNT(*) FROM persisted_session WHERE last_used_at < ?",
            (cutoff_iso,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row[0] or 0

    async def delete_sessions_before(self, cutoff_date: datetime) -> int:
        """Delete sessions with last_used_at before cutoff_date. Returns count deleted."""
        async with self._lock:
            db = self._require()
            cutoff_iso = cutoff_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            cur = await db.execute(
                "DELETE FROM persisted_session WHERE last_used_at < ?",
                (cutoff_iso,),
            )
            removed = cur.rowcount or 0
            await cur.close()
            await db.commit()
            return removed

    @staticmethod
    def _safe_serialize(context_data: Dict[str, Any]) -> str:
        """Serialize dict to JSON; redact obvious secrets via secrets_scanner."""
        try:
            from deile.security.secrets_scanner import SecretsScanner

            scanner = SecretsScanner()

            def _walk(obj: Any) -> Any:
                if isinstance(obj, str):
                    try:
                        return scanner.redact(obj)
                    except Exception:
                        return obj
                if isinstance(obj, dict):
                    return {k: _walk(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_walk(v) for v in obj]
                if isinstance(obj, tuple):
                    return [_walk(v) for v in obj]
                return obj

            redacted = _walk(context_data)
            return json.dumps(redacted, default=str, ensure_ascii=False)
        except Exception:
            try:
                return json.dumps(context_data, default=str, ensure_ascii=False)
            except Exception:
                return "{}"
