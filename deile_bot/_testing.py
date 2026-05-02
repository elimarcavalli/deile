"""Public test fakes — usable from any package's test suite."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.envelope import (
    Attachment,
    AttachmentKind,
    BotUser,
    Channel,
    ChannelScope,
    MessageEnvelope,
    ReplyContext,
)
from deile_bot.foundation.exceptions import CapabilityNotSupported, ProviderError
from deile_bot.providers.base import InboundCallback, ProviderAdapter


_FAKE_CAPABILITIES = ProviderCapabilities(
    can_edit_message=True,
    can_react=True,
    can_send_dm=True,
    can_threads=True,
    can_polls=False,
    can_inline_keyboards=False,
    can_slash_commands=True,
    can_voice_messages=False,
    can_send_typing=True,
    can_fetch_user_profile=True,
    has_conversation_window=False,
    max_message_chars=2000,
    max_attachments_per_message=10,
    supported_attachment_kinds=frozenset(
        {AttachmentKind.IMAGE, AttachmentKind.FILE, AttachmentKind.VIDEO, AttachmentKind.AUDIO}
    ),
)


def make_user(
    *,
    bot_user_id: Optional[str] = None,
    provider: str = "fake",
    provider_user_id: str = "u-1",
    display_name: str = "alice",
    is_bot: bool = False,
) -> BotUser:
    return BotUser(
        bot_user_id=bot_user_id or f"01HFAKE-{provider_user_id}",
        provider=provider,
        provider_user_id=provider_user_id,
        display_name=display_name,
        is_bot=is_bot,
    )


def make_channel(
    *,
    provider: str = "fake",
    provider_channel_id: str = "c-1",
    name: Optional[str] = "test-channel",
    scope: ChannelScope = ChannelScope.DM,
    parent_channel_id: Optional[str] = None,
) -> Channel:
    return Channel(
        provider=provider,
        provider_channel_id=provider_channel_id,
        name=name,
        scope=scope,
        parent_channel_id=parent_channel_id,
    )


def make_envelope(
    *,
    text: str = "olá",
    author: Optional[BotUser] = None,
    channel: Optional[Channel] = None,
    message_id: Optional[str] = None,
    sent_at: Optional[datetime] = None,
    markup: Optional[MarkupAST] = None,
    attachments: Tuple[Attachment, ...] = (),
    reply: Optional[ReplyContext] = None,
    mentions: Tuple[BotUser, ...] = (),
    raw: Optional[Mapping[str, Any]] = None,
) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=message_id or f"m-{uuid.uuid4().hex[:8]}",
        channel=channel or make_channel(),
        author=author or make_user(),
        sent_at=sent_at or datetime.now(timezone.utc),
        text=text,
        markup=markup,
        attachments=attachments,
        reply=reply,
        mentions=mentions,
        raw=MappingProxyType(dict(raw or {})),
    )


class FakeProviderAdapter(ProviderAdapter):
    """In-memory adapter for tests. Captures sends in `inbox`."""

    name = "fake"
    capabilities = _FAKE_CAPABILITIES

    def __init__(
        self,
        *,
        capabilities: Optional[ProviderCapabilities] = None,
        self_user_id: str = "fake-bot-self",
    ):
        if capabilities is not None:
            self.capabilities = capabilities
        self.inbox: List[Dict[str, Any]] = []
        self.dms: List[Dict[str, Any]] = []
        self.reactions: List[Dict[str, Any]] = []
        self.edits: List[Dict[str, Any]] = []
        self.typing_calls: List[str] = []
        self._self_user_id = self_user_id
        self._send_counter = 0
        self._fail_until_call: int = 0
        self._fail_with: Optional[Exception] = None
        self.on_inbound: Optional[InboundCallback] = None
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def fail_send_n_times(self, n: int, *, exc: Optional[Exception] = None) -> None:
        """Make the next N send_message calls raise `exc` (default ProviderError)."""
        self._fail_until_call = n
        self._fail_with = exc or ProviderError("fake adapter forced failure")

    async def send_message(
        self,
        channel: Channel,
        text: str,
        reply_to: Optional[str] = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._fail_until_call > 0:
            self._fail_until_call -= 1
            assert self._fail_with is not None
            raise self._fail_with
        self._send_counter += 1
        msg_id = f"sent-{self._send_counter}"
        record = {
            "channel": channel,
            "text": text,
            "reply_to": reply_to,
            "attachments": tuple(attachments),
            "message_id": msg_id,
        }
        self.inbox.append(record)
        return msg_id

    async def edit_message(self, channel: Channel, message_id: str, new_text: str) -> None:
        if not self.capabilities.can_edit_message:
            raise CapabilityNotSupported(f"{self.name} cannot edit messages")
        self.edits.append({"channel": channel, "message_id": message_id, "new_text": new_text})

    async def react(self, channel: Channel, message_id: str, emoji: str) -> None:
        if not self.capabilities.can_react:
            raise CapabilityNotSupported(f"{self.name} cannot react")
        self.reactions.append({"channel": channel, "message_id": message_id, "emoji": emoji})

    async def send_dm(
        self,
        user: BotUser,
        text: str,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if not self.capabilities.can_send_dm:
            raise CapabilityNotSupported(f"{self.name} cannot send DM")
        msg_id = f"dm-{len(self.dms) + 1}"
        self.dms.append(
            {"user": user, "text": text, "attachments": tuple(attachments), "message_id": msg_id}
        )
        return msg_id

    async def fetch_user_profile(self, user: BotUser) -> Mapping[str, Any]:
        if not self.capabilities.can_fetch_user_profile:
            raise CapabilityNotSupported(f"{self.name} cannot fetch profile")
        return {"display_name": user.display_name, "provider_user_id": user.provider_user_id}

    async def send_typing(self, channel: Channel) -> None:
        if not self.capabilities.can_send_typing:
            return
        self.typing_calls.append(channel.provider_channel_id)

    async def inject(self, env: MessageEnvelope) -> None:
        """Simulate provider receiving an inbound message."""
        if self.on_inbound is None:
            raise RuntimeError("on_inbound callback not set")
        await self.on_inbound(env, self)


class FakeAgentMetaProvider:
    """Trivial AgentMetaProvider for tests."""

    def __init__(
        self,
        tools: Optional[List[Any]] = None,
        models: Optional[List[str]] = None,
        personas: Optional[List[str]] = None,
    ):
        from deile_bot.foundation.agent_meta import ToolMeta

        if tools is None:
            tools = [
                ToolMeta(name="echo", description="echoes input"),
                ToolMeta(name="lookup", description="looks up info"),
            ]
        self._tools = tools
        self._models = models or ["deepseek:deepseek-chat", "anthropic:claude-test"]
        self._personas = personas or ["developer", "host"]

    async def list_tools(self) -> List[Any]:
        return list(self._tools)

    async def list_models(self) -> List[str]:
        return list(self._models)

    async def list_personas(self) -> List[str]:
        return list(self._personas)
