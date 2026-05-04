"""messaging.discord_send_dm — send a private DM to a single user.

DMs are HIGH-risk because they reach a user directly, bypassing channel
moderation. The tool requires explicit approval via ApprovalSystem.
"""

from __future__ import annotations

from typing import Any, Dict

from ..base import SecurityLevel
from ._base import MessagingTool


class DiscordSendDMTool(MessagingTool):
    tool_name = "discord_send_dm"
    description_text = (
        "Send a Direct Message to a single Discord user via the deile-bot daemon. "
        "Provide either `user_id` (provider snowflake) or `bot_user_id` (DEILE-internal "
        "ULID). Requires explicit approval — a confirmation prompt is raised."
    )
    parameters: Dict[str, Any] = {
        "user_id": {
            "type": "string",
            "description": "Discord user ID (snowflake). Provide this OR bot_user_id, not both.",
        },
        "bot_user_id": {
            "type": "string",
            "description": "Internal deile-bot user ULID. Provide this OR user_id, not both.",
        },
        "text": {
            "type": "string",
            "description": "DM body. UTF-8, plain text. Discord-formatted markdown is allowed.",
        },
    }
    required_params = ["text"]
    security_level = SecurityLevel.DANGEROUS
    require_approval = True
    approval_risk = "high"

    async def _perform(self, facade, args):
        result = await facade.dm_send(
            user_id=args.get("user_id"),
            bot_user_id=args.get("bot_user_id"),
            text=str(args["text"]),
        )
        return {
            "message_id": result.message_id,
            "user_id": result.user_id,
            "sent_at": result.sent_at.isoformat(),
        }

    def _success_message(self, data, args):
        return f"sent DM {data['message_id']} to user {data['user_id']}"
