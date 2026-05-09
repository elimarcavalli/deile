"""Mensageria proativa — tools que falam com o deilebot daemon.

Each tool maps onto one outbound operation of the deilebot
control-plane. Behaviour shared by all of them (permission check,
audit log, optional approval gate, tool result envelope, error
mapping) lives in `_base.MessagingTool`.

Auto-discovery:
- The tools register only when both
  (a) `deilebot` is installed (extra `bot`), and
  (b) `DEILE_BOT_ENDPOINT` + `DEILE_BOT_AUTH_TOKEN` are set.
- When either condition fails, `register_messaging_tools(registry)`
  returns 0 silently (no warnings, no broken state).
"""

from ._base import MessagingTool
from .auto_discover import register_messaging_tools
from .discord_get_user_profile import DiscordGetUserProfileTool
from .discord_mention_role import DiscordMentionRoleTool
from .discord_pin_message import DiscordPinMessageTool
from .discord_react import DiscordReactTool
from .discord_send_dm import DiscordSendDMTool
from .discord_send_message import DiscordSendMessageTool
from .discord_start_thread import DiscordStartThreadTool
from .whatsapp_send_template import WhatsAppSendTemplateTool

__all__ = [
    "MessagingTool",
    "DiscordSendMessageTool",
    "DiscordSendDMTool",
    "DiscordReactTool",
    "DiscordStartThreadTool",
    "DiscordPinMessageTool",
    "DiscordMentionRoleTool",
    "DiscordGetUserProfileTool",
    "WhatsAppSendTemplateTool",
    "register_messaging_tools",
]
