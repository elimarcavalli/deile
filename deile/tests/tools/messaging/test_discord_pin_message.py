"""discord_pin_message — happy path + nonexistent message."""

from __future__ import annotations

from deile.integrations.bot.client import BotClientUpstreamError
from deile.tools.messaging import DiscordPinMessageTool

from .conftest import FakeBotClient, make_context


async def test_pin_succeeds(fake_client, fake_permission, fake_audit):
    tool = DiscordPinMessageTool()
    ctx = make_context(
        args={"channel_id": "1", "message_id": "2"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    assert result.is_success


async def test_pin_message_not_found_returns_typed_error(fake_permission, fake_audit):
    fc = FakeBotClient(
        raise_on={"message_pin": BotClientUpstreamError("nope", code="UPSTREAM_ERROR")}
    )
    tool = DiscordPinMessageTool()
    result = await tool.execute(
        make_context(
            args={"channel_id": "1", "message_id": "missing"},
            fake_client=fc,
            permission=fake_permission,
            audit=fake_audit,
        )
    )
    assert result.is_error
    assert result.metadata.get("error_code") == "BOT_UPSTREAM"
