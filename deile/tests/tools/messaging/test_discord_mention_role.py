"""discord_mention_role — DANGEROUS + approval gate."""

from __future__ import annotations

from deile.tools.base import SecurityLevel
from deile.tools.messaging import DiscordMentionRoleTool

from .conftest import make_context


async def test_security_is_dangerous_and_requires_approval():
    tool = DiscordMentionRoleTool()
    assert tool.schema.security_level == SecurityLevel.DANGEROUS
    assert tool.require_approval is True


async def test_mention_blocked_without_approval(
    fake_client, fake_permission, fake_audit, fake_approval_deny
):
    tool = DiscordMentionRoleTool()
    ctx = make_context(
        args={"channel_id": "1", "role_id": "9", "text": "rollout!"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_deny,
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "APPROVAL_REQUIRED"
    assert fake_client.calls == []


async def test_mention_runs_after_grant(
    fake_client, fake_permission, fake_audit, fake_approval_grant
):
    tool = DiscordMentionRoleTool()
    ctx = make_context(
        args={"channel_id": "1", "role_id": "9", "text": "rollout!"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert fake_client.calls[0]["op"] == "role_mention"
    assert fake_client.calls[0]["role_id"] == "9"
