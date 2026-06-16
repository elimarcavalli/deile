"""messaging.discord_get_user_profile — fetch a public user profile.

Read-only. SecurityLevel.SAFE — no approval, but still goes through
PermissionManager + AuditLogger so reads are accountable.
"""

from __future__ import annotations

from typing import Any, Dict

from ..base import SecurityLevel
from ._base import MessagingTool


class DiscordGetUserProfileTool(MessagingTool):
    tool_name = "discord_get_user_profile"
    description_text = "Fetch the public profile (username, display name, avatar, is_bot) for a Discord user."
    parameters: Dict[str, Any] = {
        "user_id": {
            "type": "string",
            "description": "Discord user ID (snowflake).",
        },
    }
    required_params = ["user_id"]
    security_level = SecurityLevel.SAFE
    require_approval = False

    async def _perform(self, facade, args):
        result = await facade.get_user(str(args["user_id"]))
        return {
            "user_id": result.user_id,
            "username": result.username,
            "display_name": result.display_name,
            "avatar_url": result.avatar_url,
            "is_bot": result.is_bot,
        }

    def _success_message(self, data, args):
        return f"fetched user {data['user_id']} ({data['username']})"
