"""Messenger adapter (Meta Send API, Page-based)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.envelope import (Attachment, AttachmentKind, BotUser,
                                           Channel, ChannelScope,
                                           MessageEnvelope)
from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             ProviderError)
from deile_bot.providers.base import InboundCallback, ProviderAdapter
from deile_bot.providers.meta._common.api_client import MetaApiClient
from deile_bot.providers.meta._common.settings import MetaCommonSettings

MESSENGER_CAPABILITIES = ProviderCapabilities(
    can_edit_message=False,
    can_react=True,
    can_send_dm=True,
    can_threads=False,
    can_polls=False,
    can_inline_keyboards=True,  # Quick replies
    can_slash_commands=False,
    can_voice_messages=False,
    can_send_typing=True,
    can_fetch_user_profile=True,
    has_conversation_window=True,  # 24h window + human_agent
    max_message_chars=2000,
    max_attachments_per_message=1,
    supported_attachment_kinds=frozenset({
        AttachmentKind.IMAGE, AttachmentKind.VIDEO, AttachmentKind.AUDIO, AttachmentKind.FILE,
    }),
)


class MessengerAdapter(ProviderAdapter):
    name = "messenger"
    capabilities = MESSENGER_CAPABILITIES

    def __init__(
        self,
        settings: MetaCommonSettings,
        on_inbound: Optional[InboundCallback] = None,
        *,
        runtime: Optional[Any] = None,
    ):
        self.settings = settings
        self.on_inbound = on_inbound
        self._client: Optional[MetaApiClient] = None
        self._self_user_id = settings.page_id
        self.runtime = runtime

    @property
    def self_user_id(self) -> str:
        return self._self_user_id

    async def start(self) -> None:
        token = self.settings.page_access_token.get_secret_value()
        if not token or not self.settings.page_id:
            raise ProviderError("page_access_token + page_id required", context={})
        self._client = MetaApiClient(token, self.settings.api_version)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def handle_webhook(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.on_inbound is None:
            return {"status": "no_handler"}
        # Messenger payload: entry[].messaging[]
        for entry in payload.get("entry") or []:
            for msg_event in entry.get("messaging") or []:
                env = self._normalize(msg_event)
                if env is not None:
                    await self.on_inbound(env, self)
        return {"status": "ok"}

    def _normalize(self, evt: Mapping[str, Any]) -> Optional[MessageEnvelope]:
        msg = evt.get("message") or {}
        if not msg or msg.get("is_echo"):
            return None
        sender = (evt.get("sender") or {}).get("id", "")
        if not sender:
            return None
        text = msg.get("text", "") or ""
        author = BotUser(
            bot_user_id=f"messenger-{sender}",
            provider="messenger",
            provider_user_id=str(sender),
            display_name=str(sender),
        )
        channel = Channel(
            provider="messenger",
            provider_channel_id=str(sender),
            scope=ChannelScope.DM,
        )
        ts = evt.get("timestamp")
        sent = (
            datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            if ts else datetime.now(timezone.utc)
        )
        return MessageEnvelope(
            message_id=str(msg.get("mid", f"messenger-{int(sent.timestamp())}")),
            channel=channel,
            author=author,
            sent_at=sent,
            text=text,
            markup=MarkupAST.from_plain(text),
            raw=MappingProxyType({"sender": sender, "raw_event": dict(evt)}),
        )

    async def send_message(
        self,
        channel: Channel,
        text: str,
        reply_to: Optional[str] = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._client is None:
            raise ProviderError("Messenger client not started", context={})
        return await self._client.send_text(channel.provider_channel_id, text)

    async def edit_message(self, channel: Channel, message_id: str, new_text: str) -> None:
        raise CapabilityNotSupported("Messenger does not support edit_message")

    async def react(self, channel: Channel, message_id: str, emoji: str) -> None:
        raise CapabilityNotSupported("Messenger reactions are limited; not implemented in this adapter")

    async def send_dm(
        self,
        user: BotUser,
        text: str,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._client is None:
            raise ProviderError("Messenger client not started", context={})
        return await self._client.send_text(user.provider_user_id, text)

    async def fetch_user_profile(self, user: BotUser) -> Mapping[str, Any]:
        return {"provider_user_id": user.provider_user_id}

    async def send_typing(self, channel: Channel) -> None:
        return  # No-op in this stub
