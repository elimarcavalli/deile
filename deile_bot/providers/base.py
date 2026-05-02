"""ProviderAdapter ABC — every concrete provider implements this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.envelope import (Attachment, BotUser, Channel,
                                           MessageEnvelope)
from deile_bot.foundation.exceptions import CapabilityNotSupported

InboundCallback = Callable[[MessageEnvelope, "ProviderAdapter"], Awaitable[None]]


class ProviderAdapter(ABC):
    """Base for any concrete provider adapter (Discord, Telegram, ...)."""

    name: str = "abstract"
    capabilities: ProviderCapabilities

    on_inbound: Optional[InboundCallback] = None

    @property
    def self_user_id(self) -> str:
        return getattr(self, "_self_user_id", "")

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_message(
        self,
        channel: Channel,
        text: str,
        reply_to: Optional[str] = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        """Send a message; return the provider's new message_id."""

    async def edit_message(self, channel: Channel, message_id: str, new_text: str) -> None:
        if not self.capabilities.can_edit_message:
            raise CapabilityNotSupported(f"{self.name} cannot edit messages")
        raise NotImplementedError

    async def react(self, channel: Channel, message_id: str, emoji: str) -> None:
        if not self.capabilities.can_react:
            raise CapabilityNotSupported(f"{self.name} cannot react")
        raise NotImplementedError

    async def send_dm(
        self,
        user: BotUser,
        text: str,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if not self.capabilities.can_send_dm:
            raise CapabilityNotSupported(f"{self.name} cannot send DM")
        raise NotImplementedError

    async def fetch_user_profile(self, user: BotUser) -> Mapping[str, Any]:
        if not self.capabilities.can_fetch_user_profile:
            raise CapabilityNotSupported(f"{self.name} cannot fetch user profile")
        raise NotImplementedError

    async def send_typing(self, channel: Channel) -> None:
        if not self.capabilities.can_send_typing:
            return
