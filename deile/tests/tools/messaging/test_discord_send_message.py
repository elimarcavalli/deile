"""discord_send_message — happy path, permission denied, audit emission."""

from __future__ import annotations

from deile.security.audit_logger import AuditEventType
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


async def test_disabled_facade_returns_typed_error(monkeypatch, fake_audit):
    """No facade in session AND integration disabled → BOT_INTEGRATION_DISABLED.

    The test forces the facade off explicitly. Using just the global
    facade isn't enough because the surrounding shell env or `.env`
    may have it configured.
    """
    from deile.integrations.bot import BotClientFacade, BotIntegrationSettings
    from deile.tools.messaging import _base as base_mod
    forced_facade = BotClientFacade(BotIntegrationSettings(endpoint="", auth_token=""))
    monkeypatch.setattr(base_mod, "_resolve_facade", lambda _ctx: forced_facade)

    tool = DiscordSendMessageTool()
    ctx = make_context(args={"channel_id": "1", "text": "x"}, audit=fake_audit)
    result = await tool.execute(ctx)
    assert result.is_error
    assert result.metadata.get("error_code") == "BOT_INTEGRATION_DISABLED"


async def test_security_level_is_moderate():
    tool = DiscordSendMessageTool()
    from deile.tools.base import SecurityLevel

    assert tool.schema.security_level == SecurityLevel.MODERATE


# ── Error-message refactor tests (issue #280) ───────────────────────────────


def _make_error_client(exc: Exception):
    """Return a FakeBotClient that raises `exc` on channel_post."""
    from .conftest import FakeBotClient

    return FakeBotClient(raise_on={"channel_post": exc})


def _assert_error_details(result, *, error_code: str, recoverable: bool):
    """Assert that ToolResult.metadata contains well-formed error_details."""
    assert result.is_error
    details = result.metadata.get("error_details")
    assert isinstance(details, dict), f"error_details missing or not dict: {details}"
    assert details.get("error_code") == error_code
    assert isinstance(details.get("suggestion"), str)
    assert len(details["suggestion"]) > 0
    assert details.get("recoverable") == recoverable


async def test_auth_error_format(fake_permission, fake_audit):
    """BotClientAuthError → message with tool_name + suggestion + error_details."""
    from deile.integrations.bot.client import BotClientAuthError

    tool = DiscordSendMessageTool()
    ctx = make_context(
        args={"channel_id": "100", "text": "x"},
        fake_client=_make_error_client(BotClientAuthError("bad token")),
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    _assert_error_details(result, error_code="BOT_AUTH_ERROR", recoverable=False)
    assert "discord_send_message" in result.message
    assert "autenticação" in result.message.lower()
    assert "DEILE_BOT_AUTH_TOKEN" in result.message
    # Sensitive check: no raw token value leaked
    assert "bad token" not in result.message


async def test_rate_limited_format(fake_permission, fake_audit):
    """BotClientRateLimited → message + suggestion + error_details (no retry_after)."""
    from deile.integrations.bot.client import BotClientRateLimited

    tool = DiscordSendMessageTool()
    ctx = make_context(
        args={"channel_id": "200", "text": "x"},
        fake_client=_make_error_client(BotClientRateLimited("slow down")),
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    _assert_error_details(result, error_code="BOT_RATE_LIMITED", recoverable=True)
    assert "discord_send_message" in result.message
    assert "rate-limited" in result.message.lower()
    assert "Aguarde" in result.message
    # retry_after not set → key absent
    assert "retry_after" not in result.metadata["error_details"]


async def test_rate_limited_with_retry_after(fake_permission, fake_audit):
    """BotClientRateLimited with retry_after attribute → included in error_details."""
    from deile.integrations.bot.client import BotClientRateLimited

    exc = BotClientRateLimited("slow down")
    exc.retry_after = 5.0  # simulate real client attribute

    tool = DiscordSendMessageTool()
    ctx = make_context(
        args={"channel_id": "200", "text": "x"},
        fake_client=_make_error_client(exc),
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    _assert_error_details(result, error_code="BOT_RATE_LIMITED", recoverable=True)
    assert result.metadata["error_details"]["retry_after"] == 5.0
    assert "5.0s" in result.message


async def test_not_ready_format(fake_permission, fake_audit):
    """BotClientNotReady → message + suggestion + error_details."""
    from deile.integrations.bot.client import BotClientNotReady

    tool = DiscordSendMessageTool()
    ctx = make_context(
        args={"channel_id": "300", "text": "x"},
        fake_client=_make_error_client(BotClientNotReady("starting")),
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    _assert_error_details(result, error_code="BOT_NOT_READY", recoverable=True)
    assert "discord_send_message" in result.message
    assert "pronto" in result.message.lower()
    assert "Tente novamente" in result.message


async def test_timeout_format(fake_permission, fake_audit):
    """BotClientTimeoutError → message + suggestion + error_details."""
    from deile.integrations.bot.client import BotClientTimeoutError

    tool = DiscordSendMessageTool()
    ctx = make_context(
        args={"channel_id": "400", "text": "x"},
        fake_client=_make_error_client(BotClientTimeoutError("timed out")),
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    _assert_error_details(result, error_code="BOT_TIMEOUT", recoverable=True)
    assert "discord_send_message" in result.message
    assert "timeout" in result.message.lower()
    assert "saudável" in result.message.lower()


async def test_upstream_error_format_with_channel(fake_permission, fake_audit):
    """BotClientUpstreamError → includes channel_id when available."""
    from deile.integrations.bot.client import BotClientUpstreamError

    tool = DiscordSendMessageTool()
    ctx = make_context(
        args={"channel_id": "999", "text": "test"},
        fake_client=_make_error_client(BotClientUpstreamError("discord 500")),
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    _assert_error_details(result, error_code="BOT_UPSTREAM", recoverable=True)
    assert "discord_send_message" in result.message
    assert "999" in result.message  # channel_id present
    assert "instabilidade" in result.message.lower()
    assert "Tente novamente" in result.message


def test_upstream_error_without_channel_direct():
    """BotClientUpstreamError without channel_id → generic channel reference.

    Tests _map_exception directly because discord_send_message._perform
    requires channel_id (KeyError before the bot client is reached).
    """
    from deile.integrations.bot.client import BotClientUpstreamError

    tool = DiscordSendMessageTool()
    result = tool._map_exception(
        BotClientUpstreamError("discord 500"), args={}
    )
    _assert_error_details(result, error_code="BOT_UPSTREAM", recoverable=True)
    assert "discord_send_message" in result.message
    # Without channel_id, no "canal <id>" in message — but "Discord" should appear
    assert "Discord" in result.message
    assert "falha" in result.message.lower()


async def test_unknown_error_format(fake_permission, fake_audit):
    """Unmapped exception → BOT_UNREACHABLE with diagnostic message."""
    tool = DiscordSendMessageTool()
    ctx = make_context(
        args={"channel_id": "500", "text": "x"},
        fake_client=_make_error_client(ValueError("unexpected")),
        permission=fake_permission,
        audit=fake_audit,
    )
    result = await tool.execute(ctx)
    _assert_error_details(result, error_code="BOT_UNREACHABLE", recoverable=False)
    assert "discord_send_message" in result.message
    assert "ValueError" in result.message
    assert "inesperada" in result.message.lower()


async def test_error_messages_never_leak_sensitive_data(fake_permission, fake_audit):
    """All error messages must be free of sensitive values (token, full text)."""
    from deile.integrations.bot.client import (BotClientAuthError,
                                                BotClientNotReady,
                                                BotClientRateLimited,
                                                BotClientTimeoutError,
                                                BotClientUpstreamError)

    exceptions = [
        BotClientAuthError("tok_ABC123_secret"),
        BotClientRateLimited("rate limit"),
        BotClientNotReady("not ready"),
        BotClientTimeoutError("timeout"),
        BotClientUpstreamError("error"),
        ValueError("unknown"),
    ]
    tool = DiscordSendMessageTool()
    for exc in exceptions:
        ctx = make_context(
            args={"channel_id": "1", "text": "my-secret-payload"},
            fake_client=_make_error_client(exc),
            permission=fake_permission,
            audit=fake_audit,
            # clean audit between iterations
        )
        result = await tool.execute(ctx)
        # The full message text must never appear in the error message
        assert "my-secret-payload" not in result.message, (
            f"{type(exc).__name__} message leaked text payload"
        )
        # The raw exception message with token-like content must not leak
        if isinstance(exc, BotClientAuthError):
            assert "tok_ABC123" not in result.message
