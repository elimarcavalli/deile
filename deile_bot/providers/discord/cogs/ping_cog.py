"""Sanity-check /ping cog."""

from __future__ import annotations

from typing import Any

from discord.ext import commands


class PingCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot

    @commands.hybrid_command(name="ping", description="Latência do bot")
    async def ping(self, ctx: commands.Context):
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 `{latency_ms}ms`")
