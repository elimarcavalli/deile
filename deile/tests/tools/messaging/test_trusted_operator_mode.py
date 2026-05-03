"""DEILE_BOT_APPROVAL_AUTO=1 waives the interactive approval prompt.

This is opt-in for the operator running the CLI. The waiver is audited
(WARNING severity, APPROVAL_GRANTED event) so the decision is traceable.
"""

from __future__ import annotations

import pytest

from deile.security.audit_logger import AuditEventType, SeverityLevel
from deile.tools.messaging import DiscordSendDMTool

from .conftest import make_context


async def test_default_off_still_requires_approval(
    fake_client, fake_permission, fake_audit, fake_approval_deny, monkeypatch
):
    monkeypatch.delenv("DEILE_BOT_APPROVAL_AUTO", raising=False)
    tool = DiscordSendDMTool()
    ctx = make_context(
        args={"user_id": "1", "text": "hi"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_deny,
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata["error_code"] == "APPROVAL_REQUIRED"


async def test_env_on_waives_approval_and_audits_warning(
    fake_client, fake_permission, fake_audit, fake_approval_deny, monkeypatch
):
    monkeypatch.setenv("DEILE_BOT_APPROVAL_AUTO", "1")
    tool = DiscordSendDMTool()
    ctx = make_context(
        args={"user_id": "1", "text": "hi"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_deny,  # would deny; should be ignored
    )
    result = await tool.execute(ctx)
    assert result.is_success, result.message
    # ApprovalSystem was NOT consulted
    assert fake_approval_deny.requests == []
    # The waiver audit event is recorded as a WARNING
    granted = [
        e for e in fake_audit.events
        if e.get("event_type") == AuditEventType.APPROVAL_GRANTED
    ]
    assert granted
    assert granted[0]["severity"] == SeverityLevel.WARNING
    assert granted[0]["details"].get("approval") == "auto:trusted_operator"


@pytest.mark.parametrize("val", ["true", "yes", "ON", "1"])
async def test_truthy_values_recognised(
    val, fake_client, fake_permission, fake_audit, fake_approval_deny, monkeypatch
):
    monkeypatch.setenv("DEILE_BOT_APPROVAL_AUTO", val)
    tool = DiscordSendDMTool()
    ctx = make_context(
        args={"user_id": "1", "text": "hi"},
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_deny,
    )
    result = await tool.execute(ctx)
    assert result.is_success
