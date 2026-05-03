"""DeadLetterQueue — failed-egress queue backed by ConversationStore SQLite."""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Mapping, Optional

from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.exceptions import DLQError
from deile_bot.foundation.settings import BotSettings


class DeadLetterQueue:
    def __init__(self, store: ConversationStore, settings: BotSettings):
        self._store = store
        self._settings = settings

    async def enqueue(
        self,
        provider: str,
        payload: Mapping[str, Any],
        error: str,
        attempts: int,
    ) -> int:
        return await self._store.insert_dlq(provider, payload, error, attempts)

    async def list_pending(
        self,
        *,
        provider: Optional[str] = None,
        limit: int = 100,
    ) -> List[Mapping[str, Any]]:
        return await self._store.list_dlq(provider, limit)

    async def count(self, provider: Optional[str] = None) -> int:
        return await self._store.count_dlq(provider)

    async def replay(
        self,
        sender,
        *,
        provider: Optional[str] = None,
        since: Optional[datetime] = None,
        dry_run: bool = False,
    ) -> List[Mapping[str, Any]]:
        """Re-deliver pending DLQ records via `sender(payload) -> str`.

        On success deletes record. On failure leaves it in DLQ.
        Returns the list of attempted records with their result.
        """
        records = await self.list_pending(provider=provider, limit=200)
        if since:
            records = [
                r for r in records
                if datetime.fromisoformat(r["enqueued_at"].replace("Z", "+00:00")) >= since
            ]
        results: List[Mapping[str, Any]] = []
        for rec in records:
            entry = dict(rec)
            if dry_run:
                entry["result"] = "dry_run"
                results.append(entry)
                continue
            try:
                ret = sender(rec["payload"])
                if hasattr(ret, "__await__"):
                    await ret
                await self._store.delete_dlq(rec["id"])
                entry["result"] = "ok"
            except Exception as e:
                entry["result"] = f"failed:{type(e).__name__}:{e}"
            results.append(entry)
        return results

    async def purge(self, older_than_days: int) -> int:
        if older_than_days < 0:
            raise DLQError("older_than_days must be >= 0")
        return await self._store.purge_dlq_older_than(older_than_days)
