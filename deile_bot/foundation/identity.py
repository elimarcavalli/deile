"""IdentityResolver — provider+id → stable BotUser."""

from __future__ import annotations

import secrets
import time
from typing import List, Optional

from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.envelope import BotUser

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_ulid() -> str:
    """Crockford-base32 ULID (timestamp ms + 80 random bits)."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    ts_part = ""
    for _ in range(10):
        ts_part = _ULID_ALPHABET[ts & 0x1F] + ts_part
        ts >>= 5
    rand_part = "".join(secrets.choice(_ULID_ALPHABET) for _ in range(16))
    return ts_part + rand_part


class IdentityResolver:
    """Find or create a BotUser given (provider, provider_user_id)."""

    def __init__(self, store: ConversationStore):
        self._store = store

    async def resolve(
        self,
        provider: str,
        provider_user_id: str,
        display_name: str,
        *,
        is_bot: bool = False,
    ) -> BotUser:
        if not provider_user_id:
            raise ValueError("provider_user_id must be non-empty")
        provider_user_id = str(provider_user_id)
        existing = await self._store.get_user_by_provider_id(provider, provider_user_id)
        if existing is not None:
            user = BotUser(
                bot_user_id=existing.bot_user_id,
                provider=provider,
                provider_user_id=provider_user_id,
                display_name=display_name or existing.display_name,
                is_bot=is_bot or existing.is_bot,
            )
            await self._store.upsert_user(user)
            return user
        candidate = BotUser(
            bot_user_id=_generate_ulid(),
            provider=provider,
            provider_user_id=provider_user_id,
            display_name=display_name or "unknown",
            is_bot=is_bot,
        )
        await self._store.upsert_user(candidate)
        # Re-read: under concurrent races for the same provider_user_id, our
        # candidate's INSERT may have been a no-op (ON CONFLICT keeps existing
        # bot_user_id). Always read back the canonical row.
        canonical = await self._store.get_user_by_provider_id(provider, provider_user_id)
        return canonical or candidate

    async def by_bot_user_id(self, bot_user_id: str) -> Optional[BotUser]:
        # Linear search via display_name fallback; OK for occasional admin lookups.
        users = await self._store.search_users_by_display_name("", limit=1000)
        for u in users:
            if u.bot_user_id == bot_user_id:
                return u
        return None

    async def search_by_display_name(self, q: str) -> List[BotUser]:
        return await self._store.search_users_by_display_name(q)
