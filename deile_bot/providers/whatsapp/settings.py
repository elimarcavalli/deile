"""WhatsApp Cloud API settings + capability matrix."""

from __future__ import annotations

from pydantic import SecretStr

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore[attr-defined]

    SettingsConfigDict = None  # type: ignore[assignment]


from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.envelope import AttachmentKind

WHATSAPP_CAPABILITIES = ProviderCapabilities(
    can_edit_message=False,
    can_react=True,
    can_send_dm=True,
    can_threads=False,
    can_polls=False,
    can_inline_keyboards=True,
    can_slash_commands=False,
    can_voice_messages=True,
    can_send_typing=False,
    can_fetch_user_profile=False,
    has_conversation_window=True,
    max_message_chars=4096,
    max_attachments_per_message=1,
    supported_attachment_kinds=frozenset({
        AttachmentKind.IMAGE,
        AttachmentKind.VIDEO,
        AttachmentKind.AUDIO,
        AttachmentKind.FILE,
        AttachmentKind.STICKER,
    }),
)


class WhatsAppSettings(BaseSettings):
    access_token: SecretStr = SecretStr("")
    phone_number_id: str = ""
    business_account_id: str = ""
    verify_token: SecretStr = SecretStr("")
    api_version: str = "v22.0"
    self_user_id: str = ""

    if SettingsConfigDict is not None:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_WHATSAPP_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover
        class Config:
            env_prefix = "DEILE_BOT_WHATSAPP_"
            env_file = ".env"
