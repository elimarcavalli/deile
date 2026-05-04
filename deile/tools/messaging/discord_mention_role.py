"""messaging.discord_mention_role — mention a role in a channel.

Role mentions are HIGH-risk because they push notifications to everyone
holding the role. Always behind ApprovalSystem.
"""

from __future__ import annotations

from typing import Any, Dict

from ..base import SecurityLevel
from ._base import MessagingTool


class DiscordMentionRoleTool(MessagingTool):
    tool_name = "discord_mention_role"
    description_text = (
        "Post a message in a channel that mentions a role (`@role`). "
        "All users holding the role will be notified — requires explicit approval."
    )
    parameters: Dict[str, Any] = {
        "channel_id": {
            "type": "string",
            "description": "Channel ID where the mention is posted.",
        },
        "role_id": {
            "type": "string",
            "description": "Discord role ID to mention.",
        },
        "text": {
            "type": "string",
            "description": "Optional text appended after the mention.",
        },
    }
    required_params = ["channel_id", "role_id"]
    security_level = SecurityLevel.DANGEROUS
    require_approval = True
    approval_risk = "high"

    async def _perform(self, facade, args):
        result = await facade.role_mention(
            channel_id=str(args["channel_id"]),
            role_id=str(args["role_id"]),
            text=str(args.get("text") or ""),
        )
        return {"message_id": result.message_id}

    def _success_message(self, data, args):
        return f"mentioned role {args.get('role_id')} (msg {data['message_id']})"
