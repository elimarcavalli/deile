"""EventsCog — passive Discord events (member_join, thread_create, edits)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

import discord
from discord.ext import commands

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.envelope import (BotUser, Channel, ChannelScope,
                                           MessageEnvelope)


class EventsCog(commands.Cog):
    def __init__(self, bot: Any, runtime: Any, adapter: Any):
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not self.adapter.settings.on_member_join_enabled:
            return
        target_name = self.adapter.settings.welcome_channel_name
        guild = member.guild
        ch = next((c for c in guild.text_channels if c.name == target_name), None)
        if ch is None:
            return
        prompt = (
            f"novo membro {member.display_name} entrou em #{target_name} — dê boas-vindas "
            f"curtas e personalizadas, em ate 2 frases."
        )
        author = BotUser(
            bot_user_id=f"discord-{member.id}",
            provider="discord",
            provider_user_id=str(member.id),
            display_name=member.display_name,
        )
        channel = Channel(
            provider="discord",
            provider_channel_id=str(ch.id),
            name=ch.name,
            scope=ChannelScope.GROUP,
        )
        env = MessageEnvelope(
            message_id=f"event-join-{member.id}",
            channel=channel,
            author=author,
            sent_at=datetime.now(timezone.utc),
            text=prompt,
            markup=MarkupAST.from_plain(prompt),
            raw=MappingProxyType(
                {"force_respond": True, "source": "event:on_member_join"}
            ),
        )
        await self.runtime.pipeline.handle(env, self.adapter)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        # Persist the new thread channel; do not respond.
        ch = Channel(
            provider="discord",
            provider_channel_id=str(thread.id),
            name=getattr(thread, "name", None),
            scope=ChannelScope.THREAD,
            parent_channel_id=str(thread.parent_id) if getattr(thread, "parent_id", None) else None,
        )
        await self.runtime.pipeline.store.upsert_channel(ch)
