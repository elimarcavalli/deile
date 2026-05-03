"""discord_send_message — happy path, permission denied, audit emission."""

from __future__ import annotations

import pytest

from deile.security.audit_logger import AuditEventType, SeverityLevel
from deile.tools.messaging import DiscordSendMessageTool

from .conftest import make_context


async def test_success_path_calls_facade(fake_client, fake_permission, fake_audit):
    tool = DiscordSendMessageTool()
    ctx = make_context(
        args={"channel_id": "100", "text": "hello"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    assert result.is_success, result.message
    assert result.data["message_id"] == "mid-100"
    assert result.data["channel_id"] == "100"
    # facade was called once with the right args
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["op"] == "channel_post"
    assert call["channel_id"] == "100"
    assert call["text"] == "hello"


async def test_permission_denied_short_circuits(fake_client, fake_denied_permission, fake_audit):
    tool = DiscordSendMessageTool()
    ctx = make_context(
        args={"channel_id": "100", "text": "x"},
        fake_client=fake_client,
        permission=fake_denied_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "PERMISSION_DENIED"
    # facade must NOT have been called
    assert fake_client.calls == []
    # permission was actually consulted
    assert len(fake_denied_permission.calls) == 1


async def test_emits_audit_event_on_success(fake_client, fake_permission, fake_audit):
    tool = DiscordSendMessageTool()
    await tool.execute(
        make_context(
            args={"channel_id": "55", "text": "secret-text"},
            fake_client=fake_client,
            permission=fake_permission,
            audit=fake_audit,
        )
    )
    assert any(
        e.get("event_type") == AuditEventType.TOOL_EXECUTION
        and e.get("result") == "success"
        and e.get("tool_name") == "discord_send_message"
        for e in fake_audit.events
    )
    # Raw text never appears; only the SHA hash
    for evt in fake_audit.events:
        details = evt.get("details") or {}
        assert "secret-text" not in (str(details))
        if "text_hash" in details:
            assert len(details["text_hash"]) == 8


async def test_emits_audit_event_on_permission_denied(
    fake_client, fake_denied_permission, fake_audit
):
    tool = DiscordSendMessageTool()
    await tool.execute(
        make_context(
            args={"channel_id": "77", "text": "x"},
            fake_client=fake_client,
            permission=fake_denied_permission,
            audit=fake_audit,
        )
    )
    assert any(
        e.get("event_type") == AuditEventType.PERMISSION_DENIED for e in fake_audit.events
    )


async def test_disabled_facade_returns_typed_error(fake_audit):
    """No facade in session AND no real client configured → integration disabled."""
    tool = DiscordSendMessageTool()
    ctx = make_context(args={"channel_id": "1", "text": "x"}, audit=fake_audit)
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "BOT_INTEGRATION_DISABLED"


async def test_security_level_is_moderate():
    tool = DiscordSendMessageTool()
    from deile.tools.base import SecurityLevel

    assert tool.schema.security_level == SecurityLevel.MODERATE
