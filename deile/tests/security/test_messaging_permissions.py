"""Default-permission semantics for messaging tools.

The full PermissionManager rule profile lives in
`deile/security/permissions.py`. The messaging tools' contract is:

  - if no permission_manager is wired, the tool runs (legacy default).
  - if one is wired, the tool calls `check_permission(...)` with a
    resource string of the form `messaging:<tool>:<scope>` and an
    action of `execute`. Allowed = run; denied = PERMISSION_DENIED.

These tests verify the integration shape; the rule policy itself is
configured in YAML and is not part of this PR's tests.
"""

from __future__ import annotations

import pytest

from deile.tools.messaging import DiscordSendDMTool, DiscordSendMessageTool

import importlib.util
from pathlib import Path as _P

_conftest_path = _P(__file__).resolve().parent.parent / "tools" / "messaging" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_msg_conftest", str(_conftest_path))
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)  # type: ignore[union-attr]
FakeAuditLogger = _module.FakeAuditLogger
FakeBotClient = _module.FakeBotClient
FakePermissionManager = _module.FakePermissionManager
FakeApprovalSystem = _module.FakeApprovalSystem
make_context = _module.make_context


async def test_dm_default_blocked_without_approval():
    """Without a positive approval, DM never goes out — even with permission."""
    tool = DiscordSendDMTool()
    fc = FakeBotClient()
    pm = FakePermissionManager(allow=True)
    audit = FakeAuditLogger()
    approval = FakeApprovalSystem(decision=False)
    result = await tool.execute(
        make_context(
            args={"user_id": "42", "text": "hi"},
            fake_client=fc,
            permission=pm,
            audit=audit,
            approval=approval,
        )
    )
    assert result.is_error
    assert result.metadata["error_code"] == "APPROVAL_REQUIRED"
    assert fc.calls == []


async def test_channel_post_passes_resource_to_permission_check():
    tool = DiscordSendMessageTool()
    fc = FakeBotClient()
    pm = FakePermissionManager(allow=True)
    audit = FakeAuditLogger()
    await tool.execute(
        make_context(
            args={"channel_id": "abc", "text": "hi"},
            fake_client=fc,
            permission=pm,
            audit=audit,
        )
    )
    assert pm.calls[0]["resource"].endswith(":discord_send_message:abc")
    assert pm.calls[0]["action"] == "execute"


async def test_denylist_blocks_post():
    """Permission manager returning False → tool never reaches facade."""
    tool = DiscordSendMessageTool()
    fc = FakeBotClient()
    pm = FakePermissionManager(allow=False)
    audit = FakeAuditLogger()
    result = await tool.execute(
        make_context(
            args={"channel_id": "denied", "text": "hi"},
            fake_client=fc,
            permission=pm,
            audit=audit,
        )
    )
    assert result.is_error
    assert result.metadata["error_code"] == "PERMISSION_DENIED"
    assert fc.calls == []
