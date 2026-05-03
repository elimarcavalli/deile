"""CapabilitiesCog — /capabilities renders the live snapshot for users."""

from __future__ import annotations

from typing import Any

from discord.ext import commands


class CapabilitiesCog(commands.Cog):
    def __init__(self, bot: Any, runtime: Any, adapter: Any):
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    @commands.hybrid_command(name="capabilities", description="O que o DEILE consegue fazer aqui")
    async def capabilities(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        snap = await self.runtime.capability_catalog.snapshot(
            self.adapter, self.runtime.agent_meta
        )
        formatter = self.runtime.formatters.get("discord")
        if formatter is None:
            from deile_bot.providers.discord.formatter import \
                DiscordOutputFormatter

            formatter = DiscordOutputFormatter()
        text = self.runtime.capability_catalog.render_for_user(snap, formatter)
        for chunk in formatter.split(text):
            await ctx.send(chunk)
