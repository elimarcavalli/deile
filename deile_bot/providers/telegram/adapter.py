"""TelegramAdapter — wraps python-telegram-bot Application."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from deile_bot.foundation.envelope import Attachment, BotUser, Channel
from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             ProviderError)
from deile_bot.foundation.logging import get_logger
from deile_bot.providers.base import InboundCallback, ProviderAdapter
from deile_bot.providers.telegram.normalizer import TelegramNormalizer
from deile_bot.providers.telegram.settings import (TELEGRAM_CAPABILITIES,
                                                   TelegramBotSettings)


class TelegramAdapter(ProviderAdapter):
    name = "telegram"
    capabilities = TELEGRAM_CAPABILITIES

    def __init__(
        self,
        settings: TelegramBotSettings,
        on_inbound: Optional[InboundCallback] = None,
        *,
        runtime: Optional[Any] = None,
    ):
        self.settings = settings
        self.on_inbound = on_inbound
        self.normalizer = TelegramNormalizer()
        self._app: Any = None
        self._self_user_id: str = settings.self_user_id or ""
        self._logger = get_logger("telegram.adapter")
        self.runtime: Optional[Any] = runtime

    @property
    def self_user_id(self) -> str:
        return self._self_user_id

    async def start(self) -> None:
        token = self.settings.token.get_secret_value()
        if not token:
            raise ProviderError("TELEGRAM_TOKEN not configured", context={})
        try:
            from telegram.ext import Application, MessageHandler, filters
        except ImportError as e:
            raise ProviderError(
                "python-telegram-bot not installed (extras=telegram)",
                context={},
            ) from e

        self._app = Application.builder().token(token).build()
        adapter_ref = self

        async def _on_message(update, context):
            if adapter_ref.on_inbound is None or update.message is None:
                return
            try:
                env = adapter_ref.normalizer.to_envelope(update.message)
                await adapter_ref.on_inbound(env, adapter_ref)
            except Exception:
                adapter_ref._logger.exception("on_message handler raised")

        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
        await self._app.initialize()
        await self._app.start()
        if not self.settings.use_webhook:
            await self._app.updater.start_polling(timeout=self.settings.poll_timeout_seconds)
        me = await self._app.bot.get_me()
        self._self_user_id = str(me.id)

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            if not self.settings.use_webhook and self._app.updater is not None:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception:
            self._logger.exception("telegram stop raised")

    async def send_message(
        self,
        channel: Channel,
        text: str,
        reply_to: Optional[str] = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._app is None:
            raise ProviderError("Telegram client not started", context={})
        try:
            msg = await self._app.bot.send_message(
                chat_id=int(channel.provider_channel_id),
                text=text,
                reply_to_message_id=int(reply_to) if reply_to else None,
                parse_mode="MarkdownV2",
            )
            return str(msg.message_id)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"telegram send failed: {e}", context={}) from e

    async def edit_message(self, channel: Channel, message_id: str, new_text: str) -> None:
        if not self.capabilities.can_edit_message:
            raise CapabilityNotSupported(f"{self.name} can't edit")
        if self._app is None:
            raise ProviderError("Telegram client not started", context={})
        try:
            await self._app.bot.edit_message_text(
                chat_id=int(channel.provider_channel_id),
                message_id=int(message_id),
                text=new_text,
                parse_mode="MarkdownV2",
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"telegram edit failed: {e}", context={}) from e

    async def react(self, channel: Channel, message_id: str, emoji: str) -> None:
        if not self.capabilities.can_react:
            raise CapabilityNotSupported(f"{self.name} can't react")
        if self._app is None:
            raise ProviderError("Telegram client not started", context={})
        try:
            from telegram import ReactionTypeEmoji

            await self._app.bot.set_message_reaction(
                chat_id=int(channel.provider_channel_id),
                message_id=int(message_id),
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"telegram react failed: {e}", context={}) from e

    async def send_dm(
        self,
        user: BotUser,
        text: str,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._app is None:
            raise ProviderError("Telegram client not started", context={})
        try:
            msg = await self._app.bot.send_message(
                chat_id=int(user.provider_user_id), text=text
            )
            return str(msg.message_id)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"telegram DM failed: {e}", context={}) from e

    async def fetch_user_profile(self, user: BotUser) -> Mapping[str, Any]:
        if self._app is None:
            raise ProviderError("Telegram client not started", context={})
        try:
            chat = await self._app.bot.get_chat(int(user.provider_user_id))
            return {
                "id": str(chat.id),
                "username": getattr(chat, "username", None),
                "first_name": getattr(chat, "first_name", None),
                "last_name": getattr(chat, "last_name", None),
                "bio": getattr(chat, "bio", None),
            }
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"telegram fetch_user failed: {e}", context={}) from e

    async def send_typing(self, channel: Channel) -> None:
        if self._app is None:
            return
        try:
            from telegram.constants import ChatAction

            await self._app.bot.send_chat_action(
                chat_id=int(channel.provider_channel_id), action=ChatAction.TYPING
            )
        except Exception:
            self._logger.warning("telegram send_typing failed", exc_info=True)
