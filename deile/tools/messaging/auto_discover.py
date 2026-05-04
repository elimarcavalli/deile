"""Conditional registration of messaging tools.

The standard `ToolRegistry.auto_discover()` is a one-line call: it
imports a module and grabs every concrete `Tool` subclass. That's wrong
for messaging tools, because the tools should *only* register when:

  1. `deilebot` is installed, AND
  2. `DEILE_BOT_ENDPOINT` and `DEILE_BOT_AUTH_TOKEN` are configured.

Otherwise the LLM would see (and call) tools that immediately fail at
runtime with `BOT_INTEGRATION_DISABLED`.

This module exposes `register_messaging_tools(registry)` which the
registry calls from `auto_discover()`. Callers don't import the
concrete tool classes — they just receive the count of registered tools.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..registry import ToolRegistry

logger = logging.getLogger(__name__)


_TOOL_CLASSES = (
    "DiscordSendMessageTool",
    "DiscordSendDMTool",
    "DiscordReactTool",
    "DiscordStartThreadTool",
    "DiscordPinMessageTool",
    "DiscordMentionRoleTool",
    "DiscordGetUserProfileTool",
)


def register_messaging_tools(registry: "ToolRegistry") -> int:
    """Register messaging tools on the given registry. Returns count.

    Returns 0 (silently) when the integration prerequisites are not met,
    so the registry's auto-discovery prints the same totals as before
    without spurious warnings.
    """
    from ...integrations.bot import BOT_CLIENT_AVAILABLE, get_bot_settings

    if not BOT_CLIENT_AVAILABLE:
        logger.debug("messaging tools skipped: deilebot not installed")
        return 0

    settings = get_bot_settings()
    if not settings.is_configured:
        logger.debug("messaging tools skipped: bot integration not configured")
        return 0

    # Lazy imports — keep them inside the conditional so absence of
    # deilebot never breaks the deile import chain.
    from . import (DiscordGetUserProfileTool, DiscordMentionRoleTool,
                   DiscordPinMessageTool, DiscordReactTool, DiscordSendDMTool,
                   DiscordSendMessageTool, DiscordStartThreadTool)

    candidates = [
        DiscordSendMessageTool(),
        DiscordSendDMTool(),
        DiscordReactTool(),
        DiscordStartThreadTool(),
        DiscordPinMessageTool(),
        DiscordMentionRoleTool(),
        DiscordGetUserProfileTool(),
    ]

    registered = 0
    for tool in candidates:
        if tool.name in registry:
            continue  # idempotent — register_messaging_tools is safe to call twice
        try:
            registry.register(tool)
            registered += 1
        except Exception:
            logger.exception("failed to register messaging tool %s", tool.name)
    logger.info("messaging tools registered", extra={"count": registered})
    return registered
