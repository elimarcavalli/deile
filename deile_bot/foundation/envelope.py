"""Inbound and outbound DTOs for messages, plus channel/user/attachment types.

Frozen dataclasses; safe to share across asyncio tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Optional, Tuple

from deile.common.markup_ast import MarkupAST

if TYPE_CHECKING:
    from deile_bot.foundation.interactive import InteractiveControls


_EMPTY_MAP: Mapping[str, Any] = MappingProxyType({})


class ChannelScope(str, Enum):
    DM = "DM"
    GROUP = "GROUP"
    THREAD = "THREAD"
    BROADCAST = "BROADCAST"


class AttachmentKind(str, Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    FILE = "FILE"
    STICKER = "STICKER"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class BotUser:
    bot_user_id: str
    provider: str
    provider_user_id: str
    display_name: str
    is_bot: bool = False

    def __post_init__(self) -> None:
        if not self.bot_user_id:
            raise ValueError("bot_user_id must be non-empty")
        if not self.provider:
            raise ValueError("provider must be non-empty")
        if not self.provider_user_id:
            raise ValueError("provider_user_id must be non-empty")


@dataclass(frozen=True, slots=True)
class Channel:
    provider: str
    provider_channel_id: str
    name: Optional[str] = None
    scope: ChannelScope = ChannelScope.DM
    parent_channel_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("provider must be non-empty")
        if not self.provider_channel_id:
            raise ValueError("provider_channel_id must be non-empty")
        if self.scope == ChannelScope.THREAD and not self.parent_channel_id:
            raise ValueError("THREAD scope requires parent_channel_id")


@dataclass(frozen=True, slots=True)
class Attachment:
    kind: AttachmentKind
    url: Optional[str] = None
    bytes_inline: Optional[bytes] = None
    mime: Optional[str] = None
    filename: Optional[str] = None
    size_bytes: Optional[int] = None

    @property
    def is_inline(self) -> bool:
        return self.bytes_inline is not None


@dataclass(frozen=True, slots=True)
class ReplyContext:
    replied_message_id: str
    replied_author: BotUser
    replied_excerpt: str

    def __post_init__(self) -> None:
        if not self.replied_message_id:
            raise ValueError("replied_message_id must be non-empty")


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """Inbound message normalized by an adapter."""

    message_id: str
    channel: Channel
    author: BotUser
    sent_at: datetime
    text: str
    markup: Optional[MarkupAST] = None
    attachments: Tuple[Attachment, ...] = ()
    reply: Optional[ReplyContext] = None
    mentions: Tuple[BotUser, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_MAP)

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("message_id must be non-empty")
        if self.sent_at.tzinfo is None:
            raise ValueError("sent_at must be timezone-aware (UTC recommended)")
        if not isinstance(self.attachments, tuple):
            raise TypeError("attachments must be a tuple")
        if not isinstance(self.mentions, tuple):
            raise TypeError("mentions must be a tuple")
        if not isinstance(self.raw, Mapping):
            raise TypeError("raw must be a Mapping")

    @property
    def has_attachments(self) -> bool:
        return len(self.attachments) > 0

    @property
    def is_dm(self) -> bool:
        return self.channel.scope == ChannelScope.DM

    def mentions_self(self, self_user_id: str) -> bool:
        if not self_user_id:
            return False
        return any(m.provider_user_id == self_user_id for m in self.mentions)

    @property
    def has_force_respond(self) -> bool:
        """Synthetic-message contract — see master plan §2.7."""
        try:
            return bool(self.raw.get("force_respond", False))
        except Exception:
            return False


# ─── Outbound ──────────────────────────────────────────────────────────


class OutboundIntent(str, Enum):
    FREE_TEXT = "free_text"
    TEMPLATE = "template"


@dataclass(frozen=True, slots=True)
class TemplateMessage:
    name: str
    language: str
    body_params: Tuple[str, ...] = ()
    header_params: Tuple[str, ...] = ()
    button_params: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationWindow:
    last_inbound_at: Optional[datetime]
    window_hours: int

    def __post_init__(self) -> None:
        if self.window_hours <= 0:
            raise ValueError("window_hours must be > 0")

    @property
    def is_open(self) -> bool:
        if self.last_inbound_at is None:
            return False
        from datetime import timezone
        now = datetime.now(timezone.utc)
        delta = now - self.last_inbound_at
        return delta.total_seconds() <= self.window_hours * 3600


@dataclass(frozen=True, slots=True)
class OutboundEnvelope:
    intent: OutboundIntent
    text: Optional[str] = None
    template: Optional[TemplateMessage] = None
    interactive: Optional["InteractiveControls"] = None
    attachments: Tuple[Attachment, ...] = ()
    reply_to: Optional[str] = None

    def __post_init__(self) -> None:
        if self.intent == OutboundIntent.FREE_TEXT and self.text is None:
            raise ValueError("FREE_TEXT intent requires text")
        if self.intent == OutboundIntent.TEMPLATE and self.template is None:
            raise ValueError("TEMPLATE intent requires template")

    @property
    def requires_template_window(self) -> bool:
        return self.intent == OutboundIntent.TEMPLATE
