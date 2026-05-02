"""Smoke test for HelpCog/PingCog (uses commands.Bot in-memory, no network)."""

from __future__ import annotations

import pytest

discord = pytest.importorskip("discord")
commands = pytest.importorskip("discord.ext.commands")


class TestHelpCog:
    async def test_help_cog_attaches(self):
        from deile_bot.providers.discord.cogs.help_cog import HelpCog
        from deile_bot.providers.discord.cogs.ping_cog import PingCog

        intents = discord.Intents.default()
        intents.message_content = True
        bot = commands.Bot(command_prefix="d!", intents=intents, help_command=None)
        await bot.add_cog(HelpCog(bot))
        await bot.add_cog(PingCog(bot))

        names = {c.name for c in bot.commands}
        assert "help" in names
        assert "ping" in names
        await bot.close()
