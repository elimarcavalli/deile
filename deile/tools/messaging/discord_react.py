"""messaging.discord_react — add a reaction to an existing message."""

from __future__ import annotations

from typing import Any, Dict

from ..base import SecurityLevel
from ._base import MessagingTool


class DiscordReactTool(MessagingTool):
    tool_name = "discord_react"
    description_text = (
        "Add a reaction emoji to an existing Discord message. "
        "Accepts unicode emoji (👍) or Discord custom emoji syntax (`<:name:id>`)."
    )
    parameters: Dict[str, Any] = {
        "channel_id": {
            "type": "string",
            "description": "Channel ID where the target message lives.",
        },
        "message_id": {
            "type": "string",
            "description": "Target message_id to react to.",
        },
        "emoji": {
            "type": "string",
            "description": "Unicode emoji or Discord custom emoji (`<:name:id>`).",
        },
    }
    required_params = ["channel_id", "message_id", "emoji"]
    security_level = SecurityLevel.MODERATE
    require_approval = False

    async def _perform(self, facade, args):
        result = await facade.reaction_add(
            channel_id=str(args["channel_id"]),
            message_id=str(args["message_id"]),
            emoji=str(args["emoji"]),
        )
        return {"ok": bool(getattr(result, "ok", True))}

    def _success_message(self, data, args):
        return f"reacted with {args.get('emoji')} on message {args.get('message_id')}"
