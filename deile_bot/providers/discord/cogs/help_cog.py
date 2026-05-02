"""Auto-generated /help cog (lists every slash + prefix command on the bot)."""

from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands


class HelpCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Lista comandos do bot")
    async def help(self, ctx: commands.Context):
        slash = list(self.bot.tree.get_commands())
        prefix = [c for c in self.bot.commands if c.name != "help"]
        embed = discord.Embed(title="📚 DEILE — Comandos", color=0x4F8FF7)
        if slash:
            text = "\n".join(
                f"/{c.name} — {getattr(c, 'description', '') or ''}" for c in slash
            )
            embed.add_field(name="Slash", value=text or "(none)", inline=False)
        if prefix:
            text = "\n".join(
                f"{self.bot.command_prefix}{c.name} — {c.help or c.description or ''}"
                for c in prefix
            )
            embed.add_field(name=f"Prefixo ({self.bot.command_prefix})", value=text or "(none)", inline=False)
        embed.set_footer(text="Mais detalhes via /capabilities")
        await ctx.send(embed=embed)
