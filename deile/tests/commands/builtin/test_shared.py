"""Testes para deile/commands/builtin/_shared.py

Cobre split_args, _colored_panel, error_panel, warning_panel, success_panel,
export_timestamp, get_memory_manager e emit_audit_event.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from rich.panel import Panel

from deile.commands.base import CommandContext
from deile.commands.builtin._shared import (_colored_panel, emit_audit_event,
                                            error_panel, export_timestamp,
                                            get_memory_manager, get_session_id,
                                            split_args, success_panel,
                                            warning_panel)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(args: str = "") -> CommandContext:
    ctx = CommandContext(user_input=f"/cmd {args}", args=args)
    return ctx


# ---------------------------------------------------------------------------
# split_args
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_split_args_empty_string():
    ctx = _make_context(args="")
    assert split_args(ctx) == []


@pytest.mark.unit
def test_split_args_whitespace_only():
    ctx = _make_context(args="   ")
    assert split_args(ctx) == []


@pytest.mark.unit
def test_split_args_single_word():
    ctx = _make_context(args="foo")
    assert split_args(ctx) == ["foo"]


@pytest.mark.unit
def test_split_args_multiple_words():
    ctx = _make_context(args="foo bar baz")
    assert split_args(ctx) == ["foo", "bar", "baz"]


@pytest.mark.unit
def test_split_args_no_args_attribute():
    ctx = MagicMock(spec=[])  # no 'args' attribute
    assert split_args(ctx) == []


# ---------------------------------------------------------------------------
# _colored_panel / error_panel / warning_panel / success_panel
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_colored_panel_returns_panel():
    panel = _colored_panel("msg", "title", "blue")
    assert isinstance(panel, Panel)


@pytest.mark.unit
def test_colored_panel_none_title():
    panel = _colored_panel("msg", None, "red")
    assert isinstance(panel, Panel)


@pytest.mark.unit
def test_error_panel_returns_panel():
    panel = error_panel("something went wrong")
    assert isinstance(panel, Panel)


@pytest.mark.unit
def test_error_panel_default_title():
    # default title should be "Erro"
    panel = error_panel("oops")
    assert panel.title == "Erro"


@pytest.mark.unit
def test_error_panel_custom_title():
    panel = error_panel("oops", title="Custom")
    assert panel.title == "Custom"


@pytest.mark.unit
def test_warning_panel_returns_panel():
    panel = warning_panel("watch out")
    assert isinstance(panel, Panel)


@pytest.mark.unit
def test_warning_panel_default_title_aviso():
    panel = warning_panel("watch out")
    assert panel.title == "Aviso"


@pytest.mark.unit
def test_success_panel_returns_panel():
    panel = success_panel("all good")
    assert isinstance(panel, Panel)


@pytest.mark.unit
def test_success_panel_default_title_sucesso():
    panel = success_panel("all good")
    assert panel.title == "Sucesso"


# ---------------------------------------------------------------------------
# export_timestamp
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_export_timestamp_format():
    ts = export_timestamp()
    # Must match YYYYMMDD_HHMMSS
    assert re.match(r"^\d{8}_\d{6}$", ts), f"Unexpected format: {ts}"


@pytest.mark.unit
def test_export_timestamp_is_utc():
    """export_timestamp deve usar UTC para ser consistente com export_command."""
    from datetime import datetime, timezone

    before = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    ts = export_timestamp()
    after = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Timestamp must be between before and after (same-second range)
    assert before <= ts <= after


# ---------------------------------------------------------------------------
# get_memory_manager
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_memory_manager_no_agent():
    ctx = _make_context()
    # context without agent attribute
    result = get_memory_manager(ctx)
    assert result is None


@pytest.mark.unit
def test_get_memory_manager_with_none_agent():
    ctx = _make_context()
    ctx.agent = None
    result = get_memory_manager(ctx)
    assert result is None


@pytest.mark.unit
def test_get_memory_manager_returns_manager():
    ctx = _make_context()
    mock_agent = MagicMock()
    mock_mm = MagicMock()
    mock_agent.memory_manager = mock_mm
    ctx.agent = mock_agent
    result = get_memory_manager(ctx)
    assert result is mock_mm


@pytest.mark.unit
def test_get_memory_manager_agent_without_memory_manager():
    ctx = _make_context()
    mock_agent = MagicMock(spec=[])  # no memory_manager attribute
    ctx.agent = mock_agent
    result = get_memory_manager(ctx)
    assert result is None


# ---------------------------------------------------------------------------
# get_session_id
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_get_session_id_no_session_returns_default():
    ctx = _make_context()  # no session attribute
    assert get_session_id(ctx) is None
    assert get_session_id(ctx, "default") == "default"


@pytest.mark.unit
def test_get_session_id_none_session_returns_default():
    ctx = _make_context()
    ctx.session = None
    assert get_session_id(ctx, "desconhecido") == "desconhecido"


@pytest.mark.unit
def test_get_session_id_returns_session_id():
    ctx = _make_context()
    mock_session = MagicMock()
    mock_session.session_id = "sess-123"
    ctx.session = mock_session
    assert get_session_id(ctx, "default") == "sess-123"


@pytest.mark.unit
def test_get_session_id_session_without_attr_returns_default():
    ctx = _make_context()
    ctx.session = MagicMock(spec=[])  # no session_id attribute
    assert get_session_id(ctx, "default") == "default"


# ---------------------------------------------------------------------------
# emit_audit_event
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_emit_audit_event_calls_logger():
    from deile.security.audit_logger import AuditEventType, SeverityLevel

    with patch("deile.commands.builtin._shared.logger") as mock_logger:
        with patch("deile.security.audit_logger.get_audit_logger") as mock_get:
            mock_audit = MagicMock()
            mock_get.return_value = mock_audit
            emit_audit_event(
                event_type=AuditEventType.TOOL_EXECUTION,
                severity=SeverityLevel.INFO,
                resource="test/resource",
                action="test_action",
            )
            # No warning should be emitted on success
            mock_logger.warning.assert_not_called()


@pytest.mark.unit
def test_emit_audit_event_fail_soft_on_exception():
    """emit_audit_event não deve propagar exceções — pilar 03 §6 fail-soft."""
    from deile.security.audit_logger import AuditEventType, SeverityLevel

    with patch("deile.commands.builtin._shared.logger") as mock_logger:
        # get_audit_logger is imported lazily inside the function body
        with patch("deile.security.audit_logger.get_audit_logger", side_effect=RuntimeError("db down")):
            # Should NOT raise
            emit_audit_event(
                event_type=AuditEventType.TOOL_EXECUTION,
                severity=SeverityLevel.WARNING,
                resource="test/resource",
                action="test_action",
            )
            # Should log at debug level (pilar 03 §6: audit é best-effort)
            mock_logger.debug.assert_called_once()
            assert "emit_audit_event" in mock_logger.debug.call_args[0][0]
