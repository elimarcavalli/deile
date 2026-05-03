"""get_user_profile — transversal."""

from __future__ import annotations

from typing import Any, Mapping

from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             PermissionDenied)
from deile_bot.foundation.tools.base import BotTool, get_bot_context


class GetUserProfileTool(BotTool):
    name = "get_user_profile"
    description = "Fetch a user's public profile fields from the provider."
    requires_bot_context = True

    async def run(self, ctx: Any, *, bot_user_id: str) -> Mapping[str, Any]:
        bc = get_bot_context(ctx)
        adapter = bc.get("adapter_ref")
        if adapter is None:
            raise PermissionDenied("get_user_profile requires bot context", context={})
        if not adapter.capabilities.can_fetch_user_profile:
            raise CapabilityNotSupported(f"{adapter.name} can't fetch profile")
        identity = bc.get("identity")
        if identity is None:
            raise PermissionDenied("identity not available", context={})
        target = await identity.by_bot_user_id(bot_user_id)
        if target is None:
            return {"ok": False, "error": "user_not_found"}
        profile = await adapter.fetch_user_profile(target)
        return {"ok": True, "profile": dict(profile)}
