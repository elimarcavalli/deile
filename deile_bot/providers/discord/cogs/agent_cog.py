"""AgentCog — `/deile <prompt>` invokes the bot foundation pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from deile.common.markup_ast import MarkupAST
from deile_bot.foundation.envelope import (BotUser, Channel, ChannelScope,
                                           MessageEnvelope)


class AgentCog(commands.Cog):
    def __init__(self, bot: Any, runtime: Any, adapter: Any):
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    def _make_envelope(self, ctx: commands.Context, prompt: str) -> MessageEnvelope:
        ch_obj = ctx.channel
        scope = (
            ChannelScope.DM
            if isinstance(ch_obj, discord.DMChannel)
            else ChannelScope.THREAD if isinstance(ch_obj, discord.Thread)
            else ChannelScope.GROUP
        )
        parent_id = None
        if scope == ChannelScope.THREAD and getattr(ch_obj, "parent", None):
            parent_id = str(ch_obj.parent.id)
        channel = Channel(
            provider="discord",
            provider_channel_id=str(ch_obj.id),
            name=getattr(ch_obj, "name", None),
            scope=scope,
            parent_channel_id=parent_id,
        )
        author = BotUser(
            bot_user_id=f"discord-{ctx.author.id}",
            provider="discord",
            provider_user_id=str(ctx.author.id),
            display_name=getattr(ctx.author, "display_name", None) or ctx.author.name,
            is_bot=bool(getattr(ctx.author, "bot", False)),
        )
        msg_id = str(ctx.message.id) if ctx.message else f"slash-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        return MessageEnvelope(
            message_id=msg_id,
            channel=channel,
            author=author,
            sent_at=datetime.now(timezone.utc),
            text=prompt,
            markup=MarkupAST.from_plain(prompt),
            raw=MappingProxyType({"force_respond": True, "source": "slash:/deile"}),
        )

    @commands.hybrid_command(name="deile", description="Pergunta ao agente DEILE")
    @app_commands.describe(prompt="Sua pergunta ou instrução")
    async def deile(self, ctx: commands.Context, *, prompt: str):
        await ctx.defer(ephemeral=False)
        env = self._make_envelope(ctx, prompt)
        await self.runtime.pipeline.handle(env, self.adapter)
        # For slash command interactions: the pipeline sent the response via ch.send().
        # Delete the deferred "thinking…" message so it doesn't linger / time-out.
        if ctx.interaction is not None:
            try:
                await ctx.interaction.delete_original_response()
            except Exception:
                pass
