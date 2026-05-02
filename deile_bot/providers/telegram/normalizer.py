"""Telegram update -> MessageEnvelope."""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Tuple

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.envelope import (Attachment, AttachmentKind, BotUser,
                                           Channel, ChannelScope,
                                           MessageEnvelope, ReplyContext)

_CHAT_TYPE_TO_SCOPE = {
    "private": ChannelScope.DM,
    "group": ChannelScope.GROUP,
    "supergroup": ChannelScope.GROUP,
    "channel": ChannelScope.BROADCAST,
}


def _bot_user_from_tg(u: Any) -> BotUser:
    return BotUser(
        bot_user_id=f"telegram-{u.id}",
        provider="telegram",
        provider_user_id=str(u.id),
        display_name=getattr(u, "full_name", None) or getattr(u, "username", "") or "unknown",
        is_bot=bool(getattr(u, "is_bot", False)),
    )


class TelegramNormalizer:
    def to_envelope(self, message: Any) -> MessageEnvelope:
        chat = message.chat
        scope = _CHAT_TYPE_TO_SCOPE.get(chat.type, ChannelScope.GROUP)
        channel = Channel(
            provider="telegram",
            provider_channel_id=str(chat.id),
            name=getattr(chat, "title", None) or getattr(chat, "username", None),
            scope=scope,
        )
        author = _bot_user_from_tg(message.from_user)
        attachments: Tuple[Attachment, ...] = ()
        if getattr(message, "photo", None):
            largest = message.photo[-1]
            attachments = (
                Attachment(
                    kind=AttachmentKind.IMAGE,
                    url=None,
                    mime="image/jpeg",
                    filename=None,
                    size_bytes=getattr(largest, "file_size", None),
                ),
            )
        elif getattr(message, "document", None):
            doc = message.document
            attachments = (
                Attachment(
                    kind=AttachmentKind.FILE,
                    mime=getattr(doc, "mime_type", None),
                    filename=getattr(doc, "file_name", None),
                    size_bytes=getattr(doc, "file_size", None),
                ),
            )
        reply = None
        if getattr(message, "reply_to_message", None):
            r = message.reply_to_message
            reply = ReplyContext(
                replied_message_id=str(r.message_id),
                replied_author=_bot_user_from_tg(r.from_user) if r.from_user else BotUser(
                    bot_user_id="telegram-unknown",
                    provider="telegram",
                    provider_user_id="unknown",
                    display_name="unknown",
                ),
                replied_excerpt=(getattr(r, "text", "") or "")[:200],
            )
        sent = getattr(message, "date", None) or datetime.now(timezone.utc)
        if getattr(sent, "tzinfo", None) is None:
            sent = sent.replace(tzinfo=timezone.utc)
        text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
        return MessageEnvelope(
            message_id=str(message.message_id),
            channel=channel,
            author=author,
            sent_at=sent,
            text=text,
            markup=MarkupAST.from_plain(text),
            attachments=attachments,
            reply=reply,
            mentions=(),
            raw=MappingProxyType({"chat_id": chat.id, "message_id": message.message_id}),
        )
