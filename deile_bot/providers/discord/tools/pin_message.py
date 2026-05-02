"""pin_message — Discord-only."""

from __future__ import annotations

from typing import Any, Mapping

from deile_bot.foundation.exceptions import PermissionDenied, ProviderError
from deile_bot.foundation.tools.base import BotTool, get_bot_context


class PinMessageTool(BotTool):
    name = "pin_message"
    description = "Pin a message in a Discord channel (requires Manage Messages perms)."
    requires_bot_context = True

    async def run(self, ctx: Any, *, channel_id: str, message_id: str) -> Mapping[str, Any]:
        bc = get_bot_context(ctx)
        adapter = bc.get("adapter_ref")
        if adapter is None or adapter.name != "discord":
            raise PermissionDenied("pin_message requires Discord adapter", context={})
        if adapter._client is None:
            raise ProviderError("Discord client not started", context={})
        try:
            ch = adapter._client.get_channel(int(channel_id))
            if ch is None:
                ch = await adapter._client.fetch_channel(int(channel_id))
            msg = await ch.fetch_message(int(message_id))
            await msg.pin()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"pin failed: {e}", context={}) from e
