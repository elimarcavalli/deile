"""mention_role — Discord-only."""

from __future__ import annotations

from typing import Any, Mapping

from deile_bot.foundation.exceptions import PermissionDenied, ProviderError
from deile_bot.foundation.tools.base import BotTool, get_bot_context


class MentionRoleTool(BotTool):
    name = "mention_role"
    description = "Mention a Discord role in a channel."
    requires_bot_context = True

    async def run(
        self,
        ctx: Any,
        *,
        channel_id: str,
        role_id: str,
        text: str = "",
    ) -> Mapping[str, Any]:
        bc = get_bot_context(ctx)
        adapter = bc.get("adapter_ref")
        if adapter is None or adapter.name != "discord":
            raise PermissionDenied("mention_role requires Discord adapter", context={})
        if adapter._client is None:
            raise ProviderError("Discord client not started", context={})
        try:
            ch = adapter._client.get_channel(int(channel_id))
            if ch is None:
                ch = await adapter._client.fetch_channel(int(channel_id))
            content = f"<@&{int(role_id)}> {text}".strip()
            msg = await ch.send(content=content)
            return {"ok": True, "message_id": str(msg.id)}
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"mention_role failed: {e}", context={}) from e
