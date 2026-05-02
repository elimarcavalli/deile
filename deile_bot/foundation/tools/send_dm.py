"""send_dm — transversal bot tool (any provider with can_send_dm=True)."""

from __future__ import annotations

from typing import Any, Mapping

from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             PermissionDenied)
from deile_bot.foundation.permissions import Action
from deile_bot.foundation.tools.base import BotTool, get_bot_context


class SendDMTool(BotTool):
    name = "send_dm"
    description = "Send a direct message to a known user. Requires owner/SEND_DM permission."
    requires_bot_context = True

    async def run(self, ctx: Any, *, bot_user_id: str, text: str) -> Mapping[str, Any]:
        bc = get_bot_context(ctx)
        adapter = bc.get("adapter_ref")
        if adapter is None:
            raise PermissionDenied("send_dm requires bot context with adapter_ref", context={})
        if not adapter.capabilities.can_send_dm:
            raise CapabilityNotSupported(f"{adapter.name} can't send DM")
        permissions = bc.get("permissions")
        invoker = bc.get("invoker_user")
        if permissions is None or invoker is None:
            raise PermissionDenied("send_dm requires permissions and invoker_user", context={})
        from deile_bot.foundation.envelope import ChannelScope

        decision = await permissions.check(invoker, Action.SEND_DM, scope=ChannelScope.DM)
        if not decision.allowed:
            raise PermissionDenied(decision.reason, context={"action": "SEND_DM"})
        identity = bc.get("identity")
        if identity is None:
            raise PermissionDenied("identity not available", context={})
        target = await identity.by_bot_user_id(bot_user_id)
        if target is None:
            return {"ok": False, "error": "user_not_found"}
        msg_id = await adapter.send_dm(target, text)
        return {"ok": True, "message_id": msg_id}
