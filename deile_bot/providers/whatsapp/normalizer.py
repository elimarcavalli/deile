"""WhatsApp webhook payload -> MessageEnvelope."""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.envelope import (Attachment, AttachmentKind, BotUser,
                                           Channel, ChannelScope,
                                           MessageEnvelope)


class WhatsAppNormalizer:
    """Normalize WhatsApp Cloud API webhook 'messages' object to MessageEnvelope."""

    def to_envelope(self, payload: Mapping[str, Any]) -> Optional[MessageEnvelope]:
        # Cloud API webhook structure: entry[].changes[].value.messages[]
        try:
            entries = payload.get("entry") or []
            for entry in entries:
                for change in entry.get("changes") or []:
                    value = change.get("value") or {}
                    contacts = {c["wa_id"]: c for c in (value.get("contacts") or [])}
                    for msg in value.get("messages") or []:
                        return self._one(msg, value, contacts)
            return None
        except Exception:
            return None

    def _one(self, msg: Mapping[str, Any], value: Mapping[str, Any], contacts: Mapping[str, Any]):
        wa_id = msg["from"]
        contact = contacts.get(wa_id) or {}
        author = BotUser(
            bot_user_id=f"whatsapp-{wa_id}",
            provider="whatsapp",
            provider_user_id=str(wa_id),
            display_name=(contact.get("profile") or {}).get("name") or wa_id,
        )
        phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")
        channel = Channel(
            provider="whatsapp",
            provider_channel_id=str(phone_number_id) + ":" + str(wa_id),
            scope=ChannelScope.DM,
        )
        msg_type = msg.get("type", "text")
        text = (msg.get("text") or {}).get("body", "") if msg_type == "text" else ""
        attachments = ()
        if msg_type == "image":
            attachments = (
                Attachment(kind=AttachmentKind.IMAGE, mime=(msg.get("image") or {}).get("mime_type")),
            )
        elif msg_type == "audio":
            attachments = (
                Attachment(kind=AttachmentKind.AUDIO, mime=(msg.get("audio") or {}).get("mime_type")),
            )
        elif msg_type == "document":
            d = msg.get("document") or {}
            attachments = (
                Attachment(
                    kind=AttachmentKind.FILE,
                    mime=d.get("mime_type"),
                    filename=d.get("filename"),
                ),
            )
        ts = msg.get("timestamp")
        sent = (
            datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if ts else datetime.now(timezone.utc)
        )
        return MessageEnvelope(
            message_id=str(msg["id"]),
            channel=channel,
            author=author,
            sent_at=sent,
            text=text,
            markup=MarkupAST.from_plain(text),
            attachments=attachments,
            raw=MappingProxyType({"phone_number_id": phone_number_id, "wa_id": wa_id}),
        )
