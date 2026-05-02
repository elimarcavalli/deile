"""WhatsApp adapter — webhook-driven (no gateway)."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from deile_bot.foundation.envelope import Attachment, BotUser, Channel
from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             ProviderError)
from deile_bot.foundation.logging import get_logger
from deile_bot.providers.base import InboundCallback, ProviderAdapter
from deile_bot.providers.whatsapp.api_client import WhatsAppApiClient
from deile_bot.providers.whatsapp.normalizer import WhatsAppNormalizer
from deile_bot.providers.whatsapp.settings import (WHATSAPP_CAPABILITIES,
                                                   WhatsAppSettings)


class WhatsAppAdapter(ProviderAdapter):
    name = "whatsapp"
    capabilities = WHATSAPP_CAPABILITIES

    def __init__(
        self,
        settings: WhatsAppSettings,
        on_inbound: Optional[InboundCallback] = None,
        *,
        runtime: Optional[Any] = None,
    ):
        self.settings = settings
        self.on_inbound = on_inbound
        self.normalizer = WhatsAppNormalizer()
        self._client: Optional[WhatsAppApiClient] = None
        self._self_user_id = settings.self_user_id or settings.phone_number_id
        self._logger = get_logger("whatsapp.adapter")
        self.runtime: Optional[Any] = runtime

    @property
    def self_user_id(self) -> str:
        return self._self_user_id

    async def start(self) -> None:
        token = self.settings.access_token.get_secret_value()
        if not token or not self.settings.phone_number_id:
            raise ProviderError("WhatsApp access_token + phone_number_id required", context={})
        self._client = WhatsAppApiClient(
            token, self.settings.phone_number_id, self.settings.api_version
        )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def handle_webhook(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        env = self.normalizer.to_envelope(payload)
        if env is None or self.on_inbound is None:
            return {"status": "ignored"}
        await self.on_inbound(env, self)
        return {"status": "ok"}

    async def send_message(
        self,
        channel: Channel,
        text: str,
        reply_to: Optional[str] = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._client is None:
            raise ProviderError("WhatsApp client not started", context={})
        wa_id = channel.provider_channel_id.split(":", 1)[-1]
        return await self._client.send_text(wa_id, text)

    async def send_template(self, user: BotUser, outbound: Any) -> str:
        if self._client is None:
            raise ProviderError("WhatsApp client not started", context={})
        tpl = outbound.template
        return await self._client.send_template(
            user.provider_user_id, tpl.name, tpl.language
        )

    async def edit_message(self, channel: Channel, message_id: str, new_text: str) -> None:
        raise CapabilityNotSupported("WhatsApp does not support edit_message")

    async def react(self, channel: Channel, message_id: str, emoji: str) -> None:
        if self._client is None:
            raise ProviderError("WhatsApp client not started", context={})
        wa_id = channel.provider_channel_id.split(":", 1)[-1]
        try:
            await self._client._post({
                "messaging_product": "whatsapp",
                "to": wa_id,
                "type": "reaction",
                "reaction": {"message_id": message_id, "emoji": emoji},
            })
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"whatsapp react failed: {e}", context={}) from e

    async def send_dm(
        self,
        user: BotUser,
        text: str,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._client is None:
            raise ProviderError("WhatsApp client not started", context={})
        return await self._client.send_text(user.provider_user_id, text)

    async def fetch_user_profile(self, user: BotUser) -> Mapping[str, Any]:
        raise CapabilityNotSupported("WhatsApp does not expose user profile")

    async def send_typing(self, channel: Channel) -> None:
        return  # WhatsApp Cloud API does not expose typing
