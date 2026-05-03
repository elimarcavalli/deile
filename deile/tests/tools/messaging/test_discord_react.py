"""discord_react — unicode + custom emoji."""

from __future__ import annotations

import pytest

from deile.tools.messaging import DiscordReactTool

from .conftest import make_context


@pytest.mark.parametrize("emoji", ["👍", "🎉", "<:partyparrot:1234567890>"])
async def test_react_round_trip(emoji, fake_client, fake_permission, fake_audit):
    tool = DiscordReactTool()
    ctx = make_context(
        args={"channel_id": "1", "message_id": "2", "emoji": emoji},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert fake_client.calls[0]["emoji"] == emoji


async def test_facade_error_mapped(fake_permission, fake_audit):
    from deile.integrations.bot.client import BotClientUpstreamError

    from .conftest import FakeBotClient

    fc = FakeBotClient(raise_on={"reaction_add": BotClientUpstreamError("boom", code="UPSTREAM_ERROR")})
    tool = DiscordReactTool()
    result = await tool.execute(
        make_context(
            args={"channel_id": "1", "message_id": "2", "emoji": "👍"},
            fake_client=fc,
            permission=fake_permission,
            audit=fake_audit,
        )
    )
    assert result.is_error
    assert result.metadata.get("error_code") == "BOT_UPSTREAM"
