"""Instagram Direct adapter (uses Messenger Send API on IG Business account)."""

from __future__ import annotations

from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.envelope import AttachmentKind
from deile_bot.providers.meta.messenger.adapter import MessengerAdapter

INSTAGRAM_CAPABILITIES = ProviderCapabilities(
    can_edit_message=False,
    can_react=False,
    can_send_dm=True,
    can_threads=False,
    can_polls=False,
    can_inline_keyboards=False,
    can_slash_commands=False,
    can_voice_messages=False,
    can_send_typing=False,
    can_fetch_user_profile=True,
    has_conversation_window=True,
    max_message_chars=1000,
    max_attachments_per_message=1,
    supported_attachment_kinds=frozenset({AttachmentKind.IMAGE, AttachmentKind.VIDEO}),
)


class InstagramAdapter(MessengerAdapter):
    name = "instagram"
    capabilities = INSTAGRAM_CAPABILITIES
