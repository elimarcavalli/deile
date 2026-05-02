"""Telegram-specific settings + capability matrix."""

from __future__ import annotations

from pydantic import SecretStr

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore[attr-defined]

    SettingsConfigDict = None  # type: ignore[assignment]


from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.envelope import AttachmentKind

TELEGRAM_CAPABILITIES = ProviderCapabilities(
    can_edit_message=True,
    can_react=True,
    can_send_dm=True,
    can_threads=False,
    can_polls=True,
    can_inline_keyboards=True,
    can_slash_commands=True,
    can_voice_messages=False,
    can_send_typing=True,
    can_fetch_user_profile=True,
    has_conversation_window=False,
    max_message_chars=4096,
    max_attachments_per_message=10,
    supported_attachment_kinds=frozenset({
        AttachmentKind.IMAGE,
        AttachmentKind.VIDEO,
        AttachmentKind.AUDIO,
        AttachmentKind.FILE,
        AttachmentKind.STICKER,
    }),
)


class TelegramBotSettings(BaseSettings):
    token: SecretStr = SecretStr("")
    use_webhook: bool = False
    webhook_url: str = ""
    webhook_secret: SecretStr = SecretStr("")
    poll_timeout_seconds: int = 30
    self_user_id: str = ""

    if SettingsConfigDict is not None:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_TELEGRAM_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover
        class Config:
            env_prefix = "DEILE_BOT_TELEGRAM_"
            env_file = ".env"
