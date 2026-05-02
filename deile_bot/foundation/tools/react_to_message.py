"""react_to_message — transversal."""

from __future__ import annotations

from typing import Any, Mapping

from deile_bot.foundation.envelope import Channel, ChannelScope
from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             PermissionDenied)
from deile_bot.foundation.tools.base import BotTool, get_bot_context


class ReactToMessageTool(BotTool):
    name = "react_to_message"
    description = "Add a reaction emoji to a recent message in this channel."
    requires_bot_context = True

    async def run(
        self,
        ctx: Any,
        *,
        channel_id: str,
        message_id: str,
        emoji: str,
    ) -> Mapping[str, Any]:
        bc = get_bot_context(ctx)
        adapter = bc.get("adapter_ref")
        if adapter is None:
            raise PermissionDenied("react_to_message requires bot context", context={})
        if not adapter.capabilities.can_react:
            raise CapabilityNotSupported(f"{adapter.name} can't react")
        ch = Channel(
            provider=adapter.name,
            provider_channel_id=str(channel_id),
            scope=ChannelScope.GROUP,
        )
        await adapter.react(ch, str(message_id), emoji)
        return {"ok": True}
