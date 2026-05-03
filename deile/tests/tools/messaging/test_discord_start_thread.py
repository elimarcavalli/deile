"""discord_start_thread — with and without parent message."""

from __future__ import annotations

from deile.tools.messaging import DiscordStartThreadTool

from .conftest import make_context


async def test_thread_in_channel_no_parent(fake_client, fake_permission, fake_audit):
    tool = DiscordStartThreadTool()
    ctx = make_context(
        args={"channel_id": "1", "name": "incident-2025-01-01"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert fake_client.calls[0]["parent_message_id"] is None
    assert fake_client.calls[0]["name"] == "incident-2025-01-01"


async def test_thread_anchored_on_message(fake_client, fake_permission, fake_audit):
    tool = DiscordStartThreadTool()
    ctx = make_context(
        args={"channel_id": "1", "name": "deep-dive", "parent_message_id": "777"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert fake_client.calls[0]["parent_message_id"] == "777"
