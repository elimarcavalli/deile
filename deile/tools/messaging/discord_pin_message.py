"""messaging.discord_pin_message — pin a message in a Discord channel."""

from __future__ import annotations

from typing import Any, Dict

from ..base import SecurityLevel
from ._base import MessagingTool


class DiscordPinMessageTool(MessagingTool):
    tool_name = "discord_pin_message"
    description_text = "Pin a Discord message in its channel. Requires Manage Messages on the bot's role."
    parameters: Dict[str, Any] = {
        "channel_id": {
            "type": "string",
            "description": "Channel ID where the message lives.",
        },
        "message_id": {
            "type": "string",
            "description": "ID of the message to pin.",
        },
    }
    required_params = ["channel_id", "message_id"]
    security_level = SecurityLevel.MODERATE
    require_approval = False

    async def _perform(self, facade, args):
        result = await facade.message_pin(
            channel_id=str(args["channel_id"]),
            message_id=str(args["message_id"]),
        )
        return {"ok": bool(getattr(result, "ok", True))}

    def _success_message(self, data, args):
        return f"pinned message {args.get('message_id')} in {args.get('channel_id')}"
