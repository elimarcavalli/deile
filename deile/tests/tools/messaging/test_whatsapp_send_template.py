"""whatsapp_send_template — template send tool requires explicit approval.

The tool is the DEILE-side surface for the deilebot control-plane endpoint
``POST /v1/outbound/whatsapp/send_template``. It is DANGEROUS because each
send costs Meta-tier money; ApprovalSystem gates every call.
"""

from __future__ import annotations

from deile.security.audit_logger import AuditEventType
from deile.tools.base import SecurityLevel
from deile.tools.messaging import WhatsAppSendTemplateTool

from .conftest import make_context


async def test_security_level_is_dangerous():
    tool = WhatsAppSendTemplateTool()
    assert tool.schema.security_level == SecurityLevel.DANGEROUS
    assert tool.require_approval is True
    assert tool.approval_risk == "high"


async def test_required_params():
    tool = WhatsAppSendTemplateTool()
    required = set(tool.schema.required)
    assert {"to", "template_name", "language"}.issubset(required)


async def test_approval_denied_blocks_send(
    fake_client, fake_permission, fake_audit, fake_approval_deny
):
    tool = WhatsAppSendTemplateTool()
    ctx = make_context(
        args={
            "to": "5511999998888",
            "template_name": "hello_world",
            "language": "en_US",
        },
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_deny,
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "APPROVAL_REQUIRED"
    assert fake_client.calls == []  # facade never called
    # approval system was consulted with the correct risk
    assert len(fake_approval_deny.requests) == 1
    assert fake_approval_deny.requests[0]["risk_level"] == "high"


async def test_approval_grant_sends_template(
    fake_client, fake_permission, fake_audit, fake_approval_grant
):
    tool = WhatsAppSendTemplateTool()
    ctx = make_context(
        args={
            "to": "5511999998888",
            "template_name": "appointment_reminder",
            "language": "pt_BR",
            "body_params": ["Maria", "10:00"],
            "category": "utility",
        },
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    result = await tool.execute(ctx)
    assert result.is_success, result.message
    call = fake_client.calls[0]
    assert call["op"] == "whatsapp_send_template"
    assert call["to"] == "5511999998888"
    assert call["template_name"] == "appointment_reminder"
    assert call["language"] == "pt_BR"
    assert call["body_params"] == ["Maria", "10:00"]
    assert call["category"] == "utility"
    # response payload propagated through
    assert result.data["template_name"] == "appointment_reminder"
    assert result.data["to"] == "5511999998888"
    # audit shows APPROVAL_GRANTED + TOOL_EXECUTION success
    types = [e.get("event_type") for e in fake_audit.events]
    assert AuditEventType.APPROVAL_GRANTED in types
    assert AuditEventType.TOOL_EXECUTION in types


async def test_permission_denied_skips_approval_and_send(
    fake_client, fake_denied_permission, fake_audit, fake_approval_grant
):
    """Permission gate runs before approval — denied perm short-circuits."""
    tool = WhatsAppSendTemplateTool()
    ctx = make_context(
        args={
            "to": "5511999998888",
            "template_name": "hi",
            "language": "en_US",
        },
        fake_client=fake_client,
        permission=fake_denied_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "PERMISSION_DENIED"
    assert fake_approval_grant.requests == []
    assert fake_client.calls == []


async def test_audit_redacts_phone_number(
    fake_client, fake_permission, fake_audit, fake_approval_grant
):
    """Phone numbers are PII — must never hit the audit log in plaintext."""
    tool = WhatsAppSendTemplateTool()
    phone = "5511987654321"
    ctx = make_context(
        args={
            "to": phone,
            "template_name": "hi",
            "language": "en_US",
        },
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    await tool.execute(ctx)
    for evt in fake_audit.events:
        assert phone not in str(evt), f"phone leaked into audit event: {evt}"


async def test_audit_keeps_template_name_and_category(
    fake_client, fake_permission, fake_audit, fake_approval_grant
):
    """Template name + category are operator config (not PII) — keep visible."""
    tool = WhatsAppSendTemplateTool()
    ctx = make_context(
        args={
            "to": "5511999",
            "template_name": "appointment_reminder",
            "language": "pt_BR",
            "category": "utility",
        },
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    await tool.execute(ctx)
    success_events = [
        e
        for e in fake_audit.events
        if e.get("event_type") == AuditEventType.TOOL_EXECUTION
        and e.get("result") == "success"
    ]
    assert success_events, "expected one success TOOL_EXECUTION event"
    details = success_events[0].get("details") or {}
    assert details.get("template_name") == "appointment_reminder"
    assert details.get("category") == "utility"
    assert details.get("language") == "pt_BR"


async def test_param_counts_recorded_in_audit(
    fake_client, fake_permission, fake_audit, fake_approval_grant
):
    tool = WhatsAppSendTemplateTool()
    ctx = make_context(
        args={
            "to": "5511999",
            "template_name": "appt",
            "language": "pt_BR",
            "body_params": ["a", "b", "c"],
            "header_params": ["x"],
        },
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    await tool.execute(ctx)
    success = [
        e
        for e in fake_audit.events
        if e.get("event_type") == AuditEventType.TOOL_EXECUTION
        and e.get("result") == "success"
    ]
    details = success[0]["details"]
    assert details["body_param_count"] == 3
    assert details["header_param_count"] == 1


async def test_default_category_is_utility(
    fake_client, fake_permission, fake_audit, fake_approval_grant
):
    tool = WhatsAppSendTemplateTool()
    ctx = make_context(
        args={
            "to": "5511999",
            "template_name": "hi",
            "language": "en_US",
        },
        fake_client=fake_client,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    await tool.execute(ctx)
    assert fake_client.calls[0]["category"] == "utility"


async def test_upstream_error_maps_to_bot_upstream(
    fake_permission, fake_audit, fake_approval_grant
):
    """Meta 132001 (template not found) lands at the tool as BotClientUpstreamError."""
    from deile.integrations.bot.client import BotClientUpstreamError

    from .conftest import FakeBotClient

    fake = FakeBotClient(
        raise_on={
            "whatsapp_send_template": BotClientUpstreamError(
                "Template name does not exist",
                status_code=400,
                code="UPSTREAM_ERROR",
                details={"meta_code": 132001},
            ),
        }
    )
    tool = WhatsAppSendTemplateTool()
    ctx = make_context(
        args={"to": "5511999", "template_name": "ghost", "language": "en_US"},
        fake_client=fake,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "BOT_UPSTREAM"


async def test_integration_disabled_short_circuits(
    fake_permission, fake_audit, fake_approval_grant
):
    """When facade is unavailable, no approval prompt and no send."""
    from .conftest import FakeBotClient

    fake = FakeBotClient()
    fake.disable()
    tool = WhatsAppSendTemplateTool()
    ctx = make_context(
        args={"to": "5511999", "template_name": "hi", "language": "en_US"},
        fake_client=fake,
        permission=fake_permission,
        audit=fake_audit,
        approval=fake_approval_grant,
    )
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "BOT_INTEGRATION_DISABLED"
    assert fake_approval_grant.requests == []
