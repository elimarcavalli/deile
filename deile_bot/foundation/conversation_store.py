"""SQLite-backed persistence for messages, users, channels, audit, DLQ."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

import aiosqlite

from deile_bot.foundation.envelope import (Attachment, BotUser, Channel,
                                           ChannelScope, MessageEnvelope)
from deile_bot.foundation.exceptions import ConversationStoreError

_SQL_DIR = Path(__file__).parent / "sql"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _serialize_raw(raw: Mapping[str, Any], gzip_threshold: int) -> Optional[str]:
    if not raw:
        return None
    try:
        payload = json.dumps(dict(raw), default=str, ensure_ascii=False)
    except Exception:
        return None
    if len(payload.encode("utf-8")) > gzip_threshold:
        gz = gzip.compress(payload.encode("utf-8"))
        return "gz:" + base64.b64encode(gz).decode("ascii")
    return payload


@dataclass(frozen=True)
class StoredMessage:
    id: int
    provider: str
    provider_channel_id: str
    provider_message_id: str
    direction: str
    bot_user_id: str
    text: str
    reply_to_message_id: Optional[str]
    sent_at: datetime
    persisted_at: datetime


class ConversationStore:
    """Async SQLite store. WAL + foreign keys. Single connection per instance."""

    def __init__(self, db_path: Path, *, gzip_threshold: int = 4096):
        self.db_path = Path(db_path)
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._gzip_threshold = gzip_threshold

    async def init(self) -> None:
        if self._db is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.commit()
        await self._run_migrations()

    async def _run_migrations(self) -> None:
        assert self._db is not None
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await self._db.commit()
        cursor = await self._db.execute("SELECT version FROM schema_version")
        applied = {row[0] for row in await cursor.fetchall()}
        await cursor.close()
        for sql_file in sorted(_SQL_DIR.glob("V*.sql")):
            stem = sql_file.stem
            try:
                version = int(stem.split("__")[0].lstrip("V").lstrip("0") or "0")
            except ValueError:
                continue
            if version in applied:
                continue
            sql = sql_file.read_text(encoding="utf-8")
            await self._db.executescript(sql)
            await self._db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, _utc_now_iso()),
            )
            await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise ConversationStoreError("ConversationStore not initialized; call init() first")
        return self._db

    async def upsert_user(self, user: BotUser) -> None:
        async with self._lock:
            db = self._require_db()
            now = _utc_now_iso()
            await db.execute(
                """
                INSERT INTO bot_user(bot_user_id, provider, provider_user_id, display_name,
                                     is_bot, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, provider_user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    user.bot_user_id,
                    user.provider,
                    user.provider_user_id,
                    user.display_name,
                    1 if user.is_bot else 0,
                    now,
                    now,
                ),
            )
            await db.commit()

    async def upsert_channel(self, channel: Channel) -> None:
        async with self._lock:
            db = self._require_db()
            await db.execute(
                """
                INSERT INTO channel(provider, provider_channel_id, name, scope, parent_channel_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider, provider_channel_id) DO UPDATE SET
                    name = excluded.name,
                    scope = excluded.scope,
                    parent_channel_id = excluded.parent_channel_id
                """,
                (
                    channel.provider,
                    channel.provider_channel_id,
                    channel.name,
                    channel.scope.value,
                    channel.parent_channel_id,
                ),
            )
            await db.commit()

    async def record_inbound(
        self,
        env: MessageEnvelope,
        *,
        bot_user_id_override: Optional[str] = None,
    ) -> int:
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO message(
                    provider, provider_channel_id, provider_message_id, direction,
                    bot_user_id, text, reply_to_message_id, sent_at, persisted_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    env.channel.provider,
                    env.channel.provider_channel_id,
                    env.message_id,
                    "inbound",
                    bot_user_id_override or env.author.bot_user_id,
                    env.text,
                    env.reply.replied_message_id if env.reply else None,
                    env.sent_at.astimezone(timezone.utc).isoformat(),
                    _utc_now_iso(),
                    _serialize_raw(env.raw, self._gzip_threshold),
                ),
            )
            row_id = cursor.lastrowid
            await cursor.close()
            for att in env.attachments:
                await self._record_attachment(row_id, att)
            await db.commit()
            return row_id or 0

    async def _record_attachment(self, message_pk: int, att: Attachment) -> None:
        db = self._require_db()
        bytes_b64 = None
        if att.bytes_inline and len(att.bytes_inline) <= 256 * 1024:
            bytes_b64 = base64.b64encode(att.bytes_inline).decode("ascii")
        await db.execute(
            """
            INSERT INTO attachment(message_id, kind, url, mime, filename, size_bytes,
                                   bytes_inline_b64)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_pk,
                att.kind.value,
                att.url,
                att.mime,
                att.filename,
                att.size_bytes,
                bytes_b64,
            ),
        )

    async def record_outbound(
        self,
        provider: str,
        channel: Channel,
        provider_message_id: str,
        bot_user_id: str,
        text: str,
        reply_to: Optional[str],
        sent_at: datetime,
    ) -> int:
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO message(
                    provider, provider_channel_id, provider_message_id, direction,
                    bot_user_id, text, reply_to_message_id, sent_at, persisted_at, raw_json
                ) VALUES (?, ?, ?, 'outbound', ?, ?, ?, ?, ?, NULL)
                """,
                (
                    provider,
                    channel.provider_channel_id,
                    provider_message_id,
                    bot_user_id,
                    text,
                    reply_to,
                    sent_at.astimezone(timezone.utc).isoformat(),
                    _utc_now_iso(),
                ),
            )
            row_id = cursor.lastrowid or 0
            await cursor.close()
            await db.commit()
            return row_id

    async def get_recent_messages(
        self,
        provider: str,
        channel: Channel,
        limit: int = 40,
    ) -> List[StoredMessage]:
        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT id, provider, provider_channel_id, provider_message_id,
                   direction, bot_user_id, text, reply_to_message_id,
                   sent_at, persisted_at
            FROM message
            WHERE provider = ? AND provider_channel_id = ?
            ORDER BY sent_at DESC, id DESC
            LIMIT ?
            """,
            (provider, channel.provider_channel_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        out: List[StoredMessage] = []
        for r in rows:
            out.append(
                StoredMessage(
                    id=r[0],
                    provider=r[1],
                    provider_channel_id=r[2],
                    provider_message_id=r[3],
                    direction=r[4],
                    bot_user_id=r[5],
                    text=r[6],
                    reply_to_message_id=r[7],
                    sent_at=datetime.fromisoformat(r[8].replace("Z", "+00:00"))
                    if "T" in r[8]
                    else datetime.now(timezone.utc),
                    persisted_at=datetime.fromisoformat(r[9].replace("Z", "+00:00"))
                    if "T" in r[9]
                    else datetime.now(timezone.utc),
                )
            )
        return out

    async def was_outbound_sent_for(
        self,
        provider: str,
        channel: Channel,
        in_reply_to_message_id: str,
    ) -> bool:
        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT 1 FROM message
            WHERE provider = ? AND provider_channel_id = ?
              AND direction = 'outbound' AND reply_to_message_id = ?
            LIMIT 1
            """,
            (provider, channel.provider_channel_id, in_reply_to_message_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def purge_older_than(self, days: int) -> int:
        async with self._lock:
            db = self._require_db()
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
            cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
            cursor = await db.execute(
                "DELETE FROM message WHERE sent_at < ?", (cutoff_iso,)
            )
            removed = cursor.rowcount or 0
            await cursor.close()
            await db.commit()
            return removed

    async def purge_user_messages(self, bot_user_id: str, before: Optional[datetime] = None) -> int:
        async with self._lock:
            db = self._require_db()
            if before is None:
                cursor = await db.execute(
                    "DELETE FROM message WHERE bot_user_id = ?", (bot_user_id,)
                )
            else:
                cursor = await db.execute(
                    "DELETE FROM message WHERE bot_user_id = ? AND sent_at < ?",
                    (bot_user_id, before.astimezone(timezone.utc).isoformat()),
                )
            removed = cursor.rowcount or 0
            await cursor.close()
            await db.commit()
            return removed

    async def insert_audit(
        self,
        event_type: str,
        *,
        bot_user_id: Optional[str] = None,
        provider: Optional[str] = None,
        provider_channel_id: Optional[str] = None,
        provider_message_id: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> int:
        async with self._lock:
            db = self._require_db()
            payload_json = json.dumps(dict(payload), default=str) if payload else None
            cursor = await db.execute(
                """
                INSERT INTO audit(event_type, bot_user_id, provider,
                                  provider_channel_id, provider_message_id,
                                  payload_json, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    bot_user_id,
                    provider,
                    provider_channel_id,
                    provider_message_id,
                    payload_json,
                    _utc_now_iso(),
                ),
            )
            row_id = cursor.lastrowid or 0
            await cursor.close()
            await db.commit()
            return row_id

    async def query_audit(
        self,
        *,
        event_type: Optional[str] = None,
        bot_user_id: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[Mapping[str, Any]]:
        db = self._require_db()
        sql = "SELECT event_type, bot_user_id, provider, provider_channel_id, occurred_at, payload_json FROM audit"
        clauses: List[str] = []
        params: List[Any] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if bot_user_id:
            clauses.append("bot_user_id = ?")
            params.append(bot_user_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "event_type": r[0],
                "bot_user_id": r[1],
                "provider": r[2],
                "provider_channel_id": r[3],
                "occurred_at": r[4],
                "payload": json.loads(r[5]) if r[5] else None,
            }
            for r in rows
        ]

    async def insert_dlq(
        self,
        provider: str,
        payload: Mapping[str, Any],
        last_error: str,
        attempts: int,
    ) -> int:
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                """
                INSERT INTO dlq(provider, payload_json, last_error, attempts, enqueued_at, next_retry_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    provider,
                    json.dumps(dict(payload), default=str, ensure_ascii=False),
                    last_error,
                    attempts,
                    _utc_now_iso(),
                ),
            )
            row_id = cursor.lastrowid or 0
            await cursor.close()
            await db.commit()
            return row_id

    async def list_dlq(
        self,
        provider: Optional[str] = None,
        limit: int = 100,
    ) -> List[Mapping[str, Any]]:
        db = self._require_db()
        if provider:
            cursor = await db.execute(
                "SELECT id, provider, payload_json, last_error, attempts, enqueued_at "
                "FROM dlq WHERE provider = ? ORDER BY enqueued_at DESC LIMIT ?",
                (provider, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT id, provider, payload_json, last_error, attempts, enqueued_at "
                "FROM dlq ORDER BY enqueued_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "id": r[0],
                "provider": r[1],
                "payload": json.loads(r[2]),
                "last_error": r[3],
                "attempts": r[4],
                "enqueued_at": r[5],
            }
            for r in rows
        ]

    async def delete_dlq(self, dlq_id: int) -> None:
        async with self._lock:
            db = self._require_db()
            await db.execute("DELETE FROM dlq WHERE id = ?", (dlq_id,))
            await db.commit()

    async def purge_dlq_older_than(self, days: int) -> int:
        async with self._lock:
            db = self._require_db()
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
            cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
            cursor = await db.execute(
                "DELETE FROM dlq WHERE enqueued_at < ?", (cutoff_iso,)
            )
            removed = cursor.rowcount or 0
            await cursor.close()
            await db.commit()
            return removed

    async def count_dlq(self, provider: Optional[str] = None) -> int:
        db = self._require_db()
        if provider:
            cursor = await db.execute("SELECT COUNT(*) FROM dlq WHERE provider = ?", (provider,))
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM dlq")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else 0

    async def get_user_by_provider_id(
        self,
        provider: str,
        provider_user_id: str,
    ) -> Optional[BotUser]:
        db = self._require_db()
        cursor = await db.execute(
            "SELECT bot_user_id, provider, provider_user_id, display_name, is_bot "
            "FROM bot_user WHERE provider = ? AND provider_user_id = ?",
            (provider, provider_user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        return BotUser(
            bot_user_id=row[0],
            provider=row[1],
            provider_user_id=row[2],
            display_name=row[3],
            is_bot=bool(row[4]),
        )

    async def search_users_by_display_name(self, q: str, limit: int = 20) -> List[BotUser]:
        db = self._require_db()
        cursor = await db.execute(
            "SELECT bot_user_id, provider, provider_user_id, display_name, is_bot "
            "FROM bot_user WHERE display_name LIKE ? LIMIT ?",
            (f"%{q}%", limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            BotUser(
                bot_user_id=r[0],
                provider=r[1],
                provider_user_id=r[2],
                display_name=r[3],
                is_bot=bool(r[4]),
            )
            for r in rows
        ]

    async def get_channel(
        self,
        provider: str,
        provider_channel_id: str,
    ) -> Optional[Channel]:
        db = self._require_db()
        cursor = await db.execute(
            "SELECT name, scope, parent_channel_id FROM channel "
            "WHERE provider = ? AND provider_channel_id = ?",
            (provider, provider_channel_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        return Channel(
            provider=provider,
            provider_channel_id=provider_channel_id,
            name=row[0],
            scope=ChannelScope(row[1]),
            parent_channel_id=row[2],
        )
