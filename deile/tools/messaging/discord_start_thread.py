"""messaging.discord_start_thread — open a Discord thread."""

from __future__ import annotations

from typing import Any, Dict

from ..base import SecurityLevel
from ._base import MessagingTool


class DiscordStartThreadTool(MessagingTool):
    tool_name = "discord_start_thread"
    description_text = (
        "Open a Discord thread. If `parent_message_id` is set, the thread anchors "
        "on that message; otherwise a top-level public thread is created in the channel."
    )
    parameters: Dict[str, Any] = {
        "channel_id": {
            "type": "string",
            "description": "Channel ID where the thread will be created.",
        },
        "name": {
            "type": "string",
            "description": "Thread name (truncated to 100 chars by Discord).",
        },
        "parent_message_id": {
            "type": "string",
            "description": "Optional message_id to anchor the thread on.",
        },
    }
    required_params = ["channel_id", "name"]
    security_level = SecurityLevel.MODERATE
    require_approval = False

    async def _perform(self, facade, args):
        result = await facade.thread_start(
            channel_id=str(args["channel_id"]),
            name=str(args["name"]),
            parent_message_id=args.get("parent_message_id"),
        )
        return {"thread_id": result.thread_id, "name": result.name}

    def _success_message(self, data, args):
        return f"opened thread {data['thread_id']} ({data.get('name')!r})"
