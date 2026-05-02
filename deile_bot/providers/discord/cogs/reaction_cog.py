"""ReactionCog — react with 🤖 to a message to invoke DEILE on it."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, Tuple

import discord
from discord.ext import commands

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.envelope import (BotUser, Channel, ChannelScope,
                                           MessageEnvelope)


class ReactionCog(commands.Cog):
    def __init__(self, bot: Any, runtime: Any, adapter: Any, *, cooldown_seconds: int = 30):
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter
        self._cooldown = cooldown_seconds
        self._last_trigger: Dict[Tuple[int, int], float] = {}

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if str(payload.emoji) != self.adapter.settings.reaction_trigger_emoji:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        key = (payload.channel_id, payload.user_id)
        now = time.monotonic()
        if now - self._last_trigger.get(key, 0) < self._cooldown:
            return
        self._last_trigger[key] = now
        ch = self.bot.get_channel(payload.channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(payload.channel_id)
            except Exception:
                return
        try:
            target = await ch.fetch_message(payload.message_id)
        except Exception:
            return
        scope = (
            ChannelScope.DM
            if isinstance(ch, discord.DMChannel)
            else ChannelScope.THREAD if isinstance(ch, discord.Thread)
            else ChannelScope.GROUP
        )
        parent_id = None
        if scope == ChannelScope.THREAD and getattr(ch, "parent", None):
            parent_id = str(ch.parent.id)
        channel = Channel(
            provider="discord",
            provider_channel_id=str(ch.id),
            name=getattr(ch, "name", None),
            scope=scope,
            parent_channel_id=parent_id,
        )
        author = BotUser(
            bot_user_id=f"discord-{payload.user_id}",
            provider="discord",
            provider_user_id=str(payload.user_id),
            display_name="reaction-trigger",
        )
        env = MessageEnvelope(
            message_id=str(payload.message_id),
            channel=channel,
            author=author,
            sent_at=datetime.now(timezone.utc),
            text=getattr(target, "content", "") or "",
            markup=MarkupAST.from_plain(getattr(target, "content", "") or ""),
            raw=MappingProxyType({"force_respond": True, "source": "reaction:🤖"}),
        )
        await self.runtime.pipeline.handle(env, self.adapter)
