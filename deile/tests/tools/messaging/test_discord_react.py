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


async def test_emoji_hashed_in_audit_not_plaintext(fake_client, fake_permission, fake_audit):
    """Custom emojis (`<:name:id>`) can encode opaque project metadata.
    Audit must hash them (consistent with text_hash) — never log raw."""
    tool = DiscordReactTool()
    secret_emoji = "<:secretInternalFlag:9876543210>"
    await tool.execute(
        make_context(
            args={"channel_id": "1", "message_id": "2", "emoji": secret_emoji},
            fake_client=fake_client,
            permission=fake_permission,
            audit=fake_audit,
        )
    )
    for evt in fake_audit.events:
        # the raw emoji text must NOT appear in any audit event details
        assert secret_emoji not in str(evt), f"emoji leaked into audit: {evt}"
        details = evt.get("details") or {}
        if "emoji_hash" in details:
            # hash must be 8 hex chars
            assert len(details["emoji_hash"]) == 8


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
