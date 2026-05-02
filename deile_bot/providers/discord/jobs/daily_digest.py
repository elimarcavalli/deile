"""daily_digest cron handler."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, List

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.envelope import (BotUser, Channel, ChannelScope,
                                           MessageEnvelope)

logger = logging.getLogger(__name__)


async def run(
    *,
    runtime: Any = None,
    adapter: Any = None,
    channels: List[str] = (),
    lookback_hours: int = 24,
    **_,
) -> None:
    """Send a synthetic envelope asking the agent to summarize each channel."""
    if runtime is None or adapter is None:
        logger.warning("daily_digest run() invoked without runtime/adapter")
        return
    for name in channels or []:
        ch_obj = None
        if hasattr(adapter, "_client") and adapter._client is not None:
            for guild in adapter._client.guilds:
                ch_obj = next((c for c in guild.text_channels if c.name == name), None)
                if ch_obj:
                    break
        if ch_obj is None:
            logger.warning(f"daily_digest: channel '{name}' not found")
            continue
        channel = Channel(
            provider="discord",
            provider_channel_id=str(ch_obj.id),
            name=ch_obj.name,
            scope=ChannelScope.GROUP,
        )
        author = BotUser(
            bot_user_id="discord-scheduler",
            provider="discord",
            provider_user_id="scheduler",
            display_name="scheduler",
            is_bot=True,
        )
        prompt = (
            f"Resuma os ultimos {lookback_hours}h ocorridos em #{name} em ate 5 bullets."
        )
        env = MessageEnvelope(
            message_id=f"digest-{int(datetime.now(timezone.utc).timestamp())}",
            channel=channel,
            author=author,
            sent_at=datetime.now(timezone.utc),
            text=prompt,
            markup=MarkupAST.from_plain(prompt),
            raw=MappingProxyType(
                {"force_respond": True, "source": "scheduler:daily_digest"}
            ),
        )
        await runtime.pipeline.handle(env, adapter)
