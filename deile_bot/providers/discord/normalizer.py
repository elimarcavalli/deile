"""Translate discord.Message → MessageEnvelope."""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Tuple

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.envelope import (Attachment, AttachmentKind, BotUser,
                                           Channel, ChannelScope,
                                           MessageEnvelope, ReplyContext)

_MIME_TO_KIND = {
    "image": AttachmentKind.IMAGE,
    "video": AttachmentKind.VIDEO,
    "audio": AttachmentKind.AUDIO,
}


def _kind_for_attachment(att: Any) -> AttachmentKind:
    ct = getattr(att, "content_type", "") or ""
    prefix = ct.split("/", 1)[0] if "/" in ct else ct
    return _MIME_TO_KIND.get(prefix, AttachmentKind.FILE)


def _bot_user_from(member_or_user: Any, *, fallback_provider_id: str = "") -> BotUser:
    pid = str(getattr(member_or_user, "id", "")) or fallback_provider_id
    return BotUser(
        bot_user_id=f"discord-{pid}",
        provider="discord",
        provider_user_id=pid,
        display_name=getattr(member_or_user, "display_name", None)
            or getattr(member_or_user, "name", "unknown"),
        is_bot=bool(getattr(member_or_user, "bot", False)),
    )


def _scope_for(channel: Any) -> ChannelScope:
    """Map a discord channel to ChannelScope without importing discord at module load."""
    cls_name = type(channel).__name__
    if cls_name in ("DMChannel", "PartialDMChannel"):
        return ChannelScope.DM
    if cls_name == "Thread":
        return ChannelScope.THREAD
    if cls_name in ("TextChannel", "VoiceChannel", "CategoryChannel", "StageChannel"):
        return ChannelScope.GROUP
    return ChannelScope.BROADCAST


class DiscordNormalizer:
    """discord.Message -> MessageEnvelope."""

    def to_envelope(self, message: Any) -> MessageEnvelope:
        ch_obj = message.channel
        scope = _scope_for(ch_obj)
        parent_id = None
        if scope == ChannelScope.THREAD:
            parent = getattr(ch_obj, "parent", None)
            if parent is not None:
                parent_id = str(parent.id)
        channel = Channel(
            provider="discord",
            provider_channel_id=str(ch_obj.id),
            name=getattr(ch_obj, "name", None),
            scope=scope,
            parent_channel_id=parent_id,
        )
        author = _bot_user_from(message.author)
        attachments: Tuple[Attachment, ...] = tuple(
            Attachment(
                kind=_kind_for_attachment(a),
                url=getattr(a, "url", None),
                mime=getattr(a, "content_type", None),
                filename=getattr(a, "filename", None),
                size_bytes=getattr(a, "size", None),
            )
            for a in (getattr(message, "attachments", None) or [])
        )
        reply = self._build_reply(message)
        mentions: Tuple[BotUser, ...] = tuple(
            _bot_user_from(m) for m in (getattr(message, "mentions", None) or [])
        )
        sent = getattr(message, "created_at", None) or datetime.now(timezone.utc)
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        raw = MappingProxyType(
            {
                "id": getattr(message, "id", None),
                "channel_id": getattr(ch_obj, "id", None),
                "guild_id": getattr(getattr(message, "guild", None), "id", None),
                "type": type(message).__name__,
            }
        )
        return MessageEnvelope(
            message_id=str(message.id),
            channel=channel,
            author=author,
            sent_at=sent,
            text=getattr(message, "content", "") or "",
            markup=MarkupAST.from_plain(getattr(message, "content", "") or ""),
            attachments=attachments,
            reply=reply,
            mentions=mentions,
            raw=raw,
        )

    def _build_reply(self, message: Any) -> Any:
        ref = getattr(message, "reference", None)
        if not ref or not getattr(ref, "message_id", None):
            return None
        # Try resolved cache; otherwise degrade to a stub.
        resolved = getattr(ref, "resolved", None)
        if resolved is not None and getattr(resolved, "author", None) is not None:
            try:
                return ReplyContext(
                    replied_message_id=str(ref.message_id),
                    replied_author=_bot_user_from(resolved.author),
                    replied_excerpt=(getattr(resolved, "content", "") or "")[:200],
                )
            except Exception:
                pass
        return ReplyContext(
            replied_message_id=str(ref.message_id),
            replied_author=BotUser(
                bot_user_id="discord-unknown",
                provider="discord",
                provider_user_id="unknown",
                display_name="unknown",
            ),
            replied_excerpt="",
        )
