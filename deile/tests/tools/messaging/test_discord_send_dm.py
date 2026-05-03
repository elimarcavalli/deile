"""discord_send_dm — DM tool requires explicit ApprovalSystem grant."""

from __future__ import annotations

from deile.security.audit_logger import AuditEventType
from deile.tools.base import SecurityLevel
from deile.tools.messaging import DiscordSendDMTool

from .conftest import make_context


async def test_security_level_is_dangerous():
    tool = DiscordSendDMTool()
    assert tool.schema.security_level == SecurityLevel.DANGEROUS


async def test_without_approval_request_pending_short_circuits(
    fake_client, fake_permission, fake_audit, fake_approval_deny
):
    tool = DiscordSendDMTool()
    ctx = make_context(
        args={"user_id": "elimar.ciss", "text": "psst"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_deny,
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "APPROVAL_REQUIRED"
    assert fake_client.calls == []  # facade never called
    # approval system actually consulted
    assert len(fake_approval_deny.requests) == 1
    assert fake_approval_deny.requests[0]["risk_level"] == "high"


async def test_with_approval_grant_sends_dm(
    fake_client, fake_permission, fake_audit, fake_approval_grant
):
    tool = DiscordSendDMTool()
    ctx = make_context(
        args={"user_id": "42", "text": "hi"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    result = await tool.execute(ctx)
    assert result.is_success, result.message
    assert fake_client.calls[0]["op"] == "dm_send"
    assert fake_client.calls[0]["user_id"] == "42"
    # audit shows both APPROVAL_GRANTED and TOOL_EXECUTION success
    types = [e.get("event_type") for e in fake_audit.events]
    assert AuditEventType.APPROVAL_GRANTED in types
    assert AuditEventType.TOOL_EXECUTION in types


async def test_permission_denied_skips_approval(
    fake_client, fake_denied_permission, fake_audit, fake_approval_grant
):
    """Permission check runs before approval — even with grant queued."""
    tool = DiscordSendDMTool()
    ctx = make_context(
        args={"user_id": "42", "text": "hi"},
        fake_client=fake_client,
        permission=fake_denied_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "PERMISSION_DENIED"
    assert fake_approval_grant.requests == []  # never reached approval
    assert fake_client.calls == []


async def test_audit_text_never_logs_raw_body(
    fake_client, fake_permission, fake_audit, fake_approval_grant
):
    tool = DiscordSendDMTool()
    secret = "this-text-must-not-leak"
    ctx = make_context(
        args={"user_id": "42", "text": secret},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    await tool.execute(ctx)
    for evt in fake_audit.events:
        assert secret not in str(evt)
