"""messaging.discord_send_message — post a text message in a Discord channel."""

from __future__ import annotations

from typing import Any, Dict

from ..base import SecurityLevel
from ._base import MessagingTool


class DiscordSendMessageTool(MessagingTool):
    tool_name = "discord_send_message"
    description_text = (
        "Post a plain-text message to a Discord channel via the deilebot daemon. "
        "Requires `channel_id` (snowflake or numeric string) and `text`. "
        "Optional `reply_to` references a previous message_id."
    )
    parameters: Dict[str, Any] = {
        "channel_id": {
            "type": "string",
            "description": "Discord channel ID (snowflake) where the message will be posted.",
        },
        "text": {
            "type": "string",
            "description": "Message body. UTF-8, plain text. Discord-formatted markdown is allowed.",
        },
        "reply_to": {
            "type": "string",
            "description": "Optional message_id this post replies to.",
        },
    }
    required_params = ["channel_id", "text"]
    security_level = SecurityLevel.MODERATE
    require_approval = False

    async def _perform(self, facade, args):
        result = await facade.channel_post(
            channel_id=str(args["channel_id"]),
            text=str(args["text"]),
            reply_to=args.get("reply_to"),
        )
        return {
            "message_id": result.message_id,
            "channel_id": result.channel_id,
            "sent_at": result.sent_at.isoformat(),
        }

    def _success_message(self, data, args):
        return f"posted message {data['message_id']} in channel {data['channel_id']}"
