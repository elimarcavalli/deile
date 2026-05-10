"""messaging.whatsapp_send_template — send an approved WhatsApp template.

Templates are required for any WhatsApp send outside the 24h conversation
window. Each call costs Meta-tier money (utility/marketing/authentication
have different prices) so the tool runs at SecurityLevel.DANGEROUS with
explicit ApprovalSystem gating, like the Discord DM tool.

The template name + language must match an entry the operator has both:
1. Submitted to and had approved by Meta in WhatsApp Business Manager.
2. Mirrored in the bot's `config/whatsapp_templates.yaml` catalog.

Mismatch surfaces as a ProviderError 132001 from the bot, mapped here to
``BOT_UPSTREAM`` so the operator can correct the catalog or chase the
Meta approval queue.
"""

from __future__ import annotations

from typing import Any, Dict

from ..base import SecurityLevel
from ._base import MessagingTool


class WhatsAppSendTemplateTool(MessagingTool):
    tool_name = "whatsapp_send_template"
    description_text = (
        "Send an approved WhatsApp Cloud API template message via the deilebot "
        "daemon. Use this when the conversation window is closed (>24h since "
        "the user's last inbound), or to initiate a new conversation. The "
        "template must already be approved in WhatsApp Business Manager and "
        "mirrored in the bot's catalog. Each send is metered against the "
        "Meta pricing tier of the chosen category. Requires explicit operator "
        "approval — a confirmation prompt is raised."
    )
    parameters: Dict[str, Any] = {
        "to": {
            "type": "string",
            "description": (
                "Recipient WhatsApp ID — E.164 phone number without leading '+', "
                "e.g. '5511999998888'."
            ),
        },
        "template_name": {
            "type": "string",
            "description": (
                "Exact template name as registered in WhatsApp Business Manager "
                "(snake_case). Must also exist in the bot's catalog when the "
                "template has body or header parameters."
            ),
        },
        "language": {
            "type": "string",
            "description": (
                "Meta locale code for the template (e.g. 'pt_BR', 'en_US', 'es_ES'). "
                "Each language is a separate Meta approval — the catalog enforces "
                "the (name, language) pair."
            ),
        },
        "body_params": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Ordered values for the template body placeholders ({{1}}, {{2}}, ...). "
                "Empty list when the template has no body params. Must match the "
                "count declared in the catalog."
            ),
        },
        "header_params": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Ordered values for the template header placeholders. Empty list "
                "when the template has no header params."
            ),
        },
        "category": {
            "type": "string",
            "enum": ["utility", "marketing", "authentication", "service"],
            "description": (
                "Meta pricing category of the template. Drives the "
                "bot_whatsapp_conversations_total metric the operator uses to "
                "track spend. Must match the category registered with Meta."
            ),
        },
    }
    required_params = ["to", "template_name", "language"]
    security_level = SecurityLevel.DANGEROUS
    require_approval = True
    approval_risk = "high"

    async def _perform(self, facade, args):
        result = await facade.whatsapp_send_template(
            to=str(args["to"]),
            template_name=str(args["template_name"]),
            language=str(args["language"]),
            body_params=list(args.get("body_params") or []),
            header_params=list(args.get("header_params") or []),
            category=str(args.get("category") or "utility"),
        )
        return {
            "message_id": result.message_id,
            "to": result.to,
            "template_name": result.template_name,
            "language": result.language,
            "sent_at": result.sent_at.isoformat(),
        }

    def _build_audit_payload(self, args):
        # WhatsApp recipient is a phone number — sensitive PII. Do NOT
        # plaintext it in the audit log; hash via the inherited _sha8.
        # Template name + language are operator config (not PII), keep them.
        from .._hash_utils import sha8 as _sha8

        payload = {"tool": self.tool_name}
        if isinstance(args, dict):
            if args.get("to"):
                payload["to_hash"] = _sha8(str(args["to"]))
            if args.get("template_name"):
                payload["template_name"] = args["template_name"]
            if args.get("language"):
                payload["language"] = args["language"]
            if args.get("category"):
                payload["category"] = args["category"]
            body = args.get("body_params") or []
            header = args.get("header_params") or []
            payload["body_param_count"] = len(body)
            payload["header_param_count"] = len(header)
        return payload

    def _success_message(self, data, args):
        return (
            f"sent template {data['template_name']} ({data['language']}) "
            f"to {data['to']} as {data['message_id']}"
        )
