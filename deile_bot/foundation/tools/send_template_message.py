"""send_template_message — for WhatsApp/Meta template windows."""

from __future__ import annotations

from typing import Any, Mapping

from deile_bot.foundation.envelope import (OutboundEnvelope, OutboundIntent,
                                           TemplateMessage)
from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             PermissionDenied)
from deile_bot.foundation.tools.base import BotTool, get_bot_context


class SendTemplateMessageTool(BotTool):
    name = "send_template_message"
    description = "Send an approved template message (WhatsApp/Meta) outside the 24h window."
    requires_bot_context = True

    async def run(
        self,
        ctx: Any,
        *,
        recipient_bot_user_id: str,
        template_name: str,
        language: str = "pt_BR",
        body_params: tuple = (),
    ) -> Mapping[str, Any]:
        bc = get_bot_context(ctx)
        adapter = bc.get("adapter_ref")
        if adapter is None:
            raise PermissionDenied("send_template_message requires bot context", context={})
        if not adapter.capabilities.has_conversation_window:
            raise CapabilityNotSupported(
                f"{adapter.name} does not have conversation_window — use send_dm instead"
            )
        identity = bc.get("identity")
        if identity is None:
            raise PermissionDenied("identity not available", context={})
        target = await identity.by_bot_user_id(recipient_bot_user_id)
        if target is None:
            return {"ok": False, "error": "user_not_found"}
        # Adapter must implement send_template — fall back to send_dm if not.
        send_template = getattr(adapter, "send_template", None)
        if send_template is None:
            raise CapabilityNotSupported(
                f"{adapter.name} does not implement send_template"
            )
        out = OutboundEnvelope(
            intent=OutboundIntent.TEMPLATE,
            template=TemplateMessage(
                name=template_name, language=language, body_params=tuple(body_params)
            ),
        )
        msg_id = await send_template(target, out)
        return {"ok": True, "message_id": msg_id}
