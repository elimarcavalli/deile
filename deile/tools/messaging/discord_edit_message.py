"""messaging.discord_edit_message — edit an existing Discord message.

Used by the worker pipeline to give the user live progress on a long-
running task: post a stub message ("🔧 Trabalhando..."), then edit it
as work advances ("⚙️ rodando py_compile…"), then edit one last time
with the final summary.

The tool is `MODERATE` risk — editing a previously-sent message cannot
duplicate or send unsolicited content; the only blast radius is the
content of one existing message that the bot already authored.
"""

from __future__ import annotations

from typing import Any, Dict

from ..base import SecurityLevel
from ._base import MessagingTool


class DiscordEditMessageTool(MessagingTool):
    tool_name = "discord_edit_message"
    description_text = (
        "Edit a Discord message previously sent by the bot. "
        "Requires `channel_id`, `message_id`, and the new `text`. "
        "Use to update a long-running task's status message in place."
    )
    parameters: Dict[str, Any] = {
        "channel_id": {
            "type": "string",
            "description": "Discord channel ID where the message lives.",
        },
        "message_id": {
            "type": "string",
            "description": "ID of the message to edit (must have been authored by this bot).",
        },
        "text": {
            "type": "string",
            "description": "New message body. UTF-8, plain text. Discord-formatted markdown allowed.",
        },
    }
    required_params = ["channel_id", "message_id", "text"]
    security_level = SecurityLevel.MODERATE
    require_approval = False

    async def _perform(self, facade, args):
        result = await facade.message_edit(
            channel_id=str(args["channel_id"]),
            message_id=str(args["message_id"]),
            text=str(args["text"]),
        )
        return {
            "message_id": result.message_id,
            "channel_id": result.channel_id,
            "edited_at": result.edited_at.isoformat(),
        }

    def _success_message(self, data, args):
        return f"edited message {data['message_id']} in channel {data['channel_id']}"
