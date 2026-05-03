"""Discord-specific settings.

`DISCORD_TOKEN` env var is the production source. Treated as `SecretStr` to
prevent accidental logging (F11 of master plan).
"""

from __future__ import annotations

from typing import List

from pydantic import Field, SecretStr

from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.envelope import AttachmentKind

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore[attr-defined]

    SettingsConfigDict = None  # type: ignore[assignment]


class DiscordBotSettings(BaseSettings):
    token: SecretStr = SecretStr("")
    intents_message_content: bool = True
    intents_members: bool = True
    intents_presences: bool = False
    command_prefix: str = "d!"
    slash_sync_guild_ids: List[int] = Field(default_factory=list)
    reaction_trigger_emoji: str = "🤖"
    message_edit_debounce_ms: int = 800
    on_member_join_enabled: bool = True
    welcome_channel_name: str = "welcome"
    self_user_id: str = ""

    if SettingsConfigDict is not None:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_DISCORD_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover
        class Config:
            env_prefix = "DEILE_BOT_DISCORD_"
            env_file = ".env"


# Capability matrix (00-PLAN.md §4 of discord plan)
DISCORD_CAPABILITIES = ProviderCapabilities(
    can_edit_message=True,
    can_react=True,
    can_send_dm=True,
    can_threads=True,
    can_polls=True,
    can_inline_keyboards=False,
    can_slash_commands=True,
    can_voice_messages=False,
    can_send_typing=True,
    can_fetch_user_profile=True,
    has_conversation_window=False,
    max_message_chars=2000,
    max_attachments_per_message=10,
    supported_attachment_kinds=frozenset({
        AttachmentKind.IMAGE,
        AttachmentKind.VIDEO,
        AttachmentKind.AUDIO,
        AttachmentKind.FILE,
    }),
)
