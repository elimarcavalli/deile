"""Build discord.Intents from DiscordBotSettings."""

from __future__ import annotations

from typing import Any

from deile_bot.providers.discord.settings import DiscordBotSettings


def build_intents(settings: DiscordBotSettings) -> Any:
    """Returns a `discord.Intents` configured from settings."""
    import discord

    intents = discord.Intents.default()
    intents.message_content = settings.intents_message_content
    intents.members = settings.intents_members
    intents.presences = settings.intents_presences
    return intents
