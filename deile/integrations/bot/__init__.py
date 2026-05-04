"""Adapter for the deile-bot daemon control-plane (the flecha reversa).

The DEILE CLI consumes the deile-bot daemon's local HTTP API to send
messages, DMs, reactions, threads, etc. This subpackage isolates the
HTTP plumbing from the messaging tools, so tools depend only on a
typed facade.

Public surface:

    from deile.integrations.bot import (
        BotIntegrationSettings, BotClientFacade,
        BOT_CLIENT_AVAILABLE, get_bot_client,
    )

`BOT_CLIENT_AVAILABLE` is False when the optional `deile-bot-client`
package is not installed; in that case the messaging tools auto-discover
into a no-op set, with no warning at import time.
"""

from .client import (BOT_CLIENT_AVAILABLE, BotClientFacade, get_bot_client,
                     reset_bot_client)
from .config import BotIntegrationSettings, get_bot_settings

__all__ = [
    "BOT_CLIENT_AVAILABLE",
    "BotClientFacade",
    "BotIntegrationSettings",
    "get_bot_client",
    "get_bot_settings",
    "reset_bot_client",
]
