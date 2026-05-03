"""Smoke tests for DiscordAdapter (no live discord connection)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from deile_bot.foundation.envelope import AttachmentKind
from deile_bot.foundation.exceptions import ProviderError
from deile_bot.providers.discord.adapter import DiscordAdapter
from deile_bot.providers.discord.settings import (DISCORD_CAPABILITIES,
                                                  DiscordBotSettings)


class TestAdapter:
    def test_capabilities(self):
        adapter = DiscordAdapter(DiscordBotSettings(token=SecretStr("x")))
        assert adapter.name == "discord"
        assert adapter.capabilities == DISCORD_CAPABILITIES
        assert AttachmentKind.IMAGE in adapter.capabilities.supported_attachment_kinds
        assert adapter.capabilities.max_message_chars == 2000

    async def test_start_without_token_raises(self):
        adapter = DiscordAdapter(DiscordBotSettings(token=SecretStr("")))
        with pytest.raises(ProviderError):
            await adapter.start()

    async def test_send_without_start_raises(self):
        from deile_bot._testing import make_channel

        adapter = DiscordAdapter(DiscordBotSettings(token=SecretStr("x")))
        with pytest.raises(ProviderError):
            await adapter.send_message(
                make_channel(provider="discord"), "hi"
            )
