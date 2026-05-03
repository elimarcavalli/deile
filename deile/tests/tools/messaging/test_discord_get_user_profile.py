"""discord_get_user_profile — read-only SAFE-level tool."""

from __future__ import annotations

from deile.tools.base import SecurityLevel
from deile.tools.messaging import DiscordGetUserProfileTool

from .conftest import make_context


async def test_security_level_is_safe():
    tool = DiscordGetUserProfileTool()
    assert tool.schema.security_level == SecurityLevel.SAFE
    assert tool.require_approval is False


async def test_get_user_profile(fake_client, fake_permission, fake_audit):
    tool = DiscordGetUserProfileTool()
    ctx = make_context(
        args={"user_id": "123"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert result.data["user_id"] == "123"
    assert result.data["username"] == "elimar.ciss"
