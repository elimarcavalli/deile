"""Tests: /logs command — issue #172 (encapsulamento, OOM, filtros, PTBR)."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from deile.commands.base import CommandContext
from deile.commands.builtin.logs_command import MAX_SAFE_LIMIT, LogsCommand
from deile.security.audit_logger import (AuditEventType, AuditLogger,
                                         SeverityLevel)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(content) -> str:
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=160)
    console.print(content)
    return buf.getvalue()


def _ctx(args: str = "") -> CommandContext:
    return CommandContext(user_input=f"/logs {args}".strip(), args=args)


def _fresh_audit_logger() -> AuditLogger:
    import tempfile
    tmp = tempfile.mkdtemp()
    return AuditLogger(log_dir=tmp)


def _cmd_with_logger(audit_logger: AuditLogger) -> LogsCommand:
    cmd = LogsCommand.__new__(LogsCommand)
    from deile.commands.base import DirectCommand
    from deile.config.manager import CommandConfig
    config = CommandConfig(name="logs", description="test")
    DirectCommand.__init__(cmd, config)
    cmd.audit_logger = audit_logger
    return cmd


def _log(audit_logger: AuditLogger, severity: SeverityLevel, event_type: AuditEventType = AuditEventType.TOOL_EXECUTION) -> None:
    audit_logger.log_event(
        event_type=event_type,
        severity=severity,
        actor="test_actor",
        resource="test_resource",
        action="test",
        result="success",
        details={},
    )


# ---------------------------------------------------------------------------
# Test: clear uses public method (not direct attribute mutation)
# ---------------------------------------------------------------------------


class TestClearUsesPublicMethod:
    async def test_clear_calls_clear_events(self):
        """_clear_logs() must delegate to audit_logger.clear_events(), not .recent_events.clear()."""
        al = _fresh_audit_logger()
        cmd = _cmd_with_logger(al)

        called = []
        original = al.clear_events

        def _spy() -> int:
            called.append(True)
            return original()

        al.clear_events = _spy  # type: ignore[method-assign]

        result = await cmd.execute(_ctx("clear"))
        assert result.success is True
        assert called, "clear_events() was never called — direct attribute access detected"

    async def test_clear_does_not_access_recent_events_clear_directly(self):
        """Verify implementation uses the encapsulated method."""
        import inspect

        from deile.commands.builtin.logs_command import LogsCommand as LC
        source = inspect.getsource(LC._clear_logs)
        assert "recent_events.clear()" not in source, (
            "Found recent_events.clear() in _clear_logs — must use audit_logger.clear_events()"
        )


# ---------------------------------------------------------------------------
# Test: clear returns real count
# ---------------------------------------------------------------------------


class TestClearReturnsRealCount:
    async def test_clear_shows_correct_count(self):
        al = _fresh_audit_logger()
        initial_count = len(al.recent_events)
        for _ in range(5):
            _log(al, SeverityLevel.INFO)
        added = 5
        expected = initial_count + added

        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("clear"))
        rendered = _render(result.content)
        assert str(expected) in rendered, f"Expected count {expected} in output: {rendered}"

    async def test_clear_count_matches_clear_events_return(self):
        al = _fresh_audit_logger()
        for _ in range(3):
            _log(al, SeverityLevel.INFO)
        total_before = len(al.recent_events)

        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("clear"))
        assert result.success is True
        rendered = _render(result.content)
        assert str(total_before) in rendered

    async def test_clear_empties_logger(self):
        al = _fresh_audit_logger()
        for _ in range(5):
            _log(al, SeverityLevel.INFO)
        cmd = _cmd_with_logger(al)
        await cmd.execute(_ctx("clear"))
        assert len(al.recent_events) == 0


# ---------------------------------------------------------------------------
# Test: recent limit capped at MAX_SAFE_LIMIT
# ---------------------------------------------------------------------------


class TestRecentLimitCappedAtMaxSafe:
    async def test_max_safe_limit_constant_is_500(self):
        assert MAX_SAFE_LIMIT == 500

    async def test_request_above_limit_is_capped(self):
        al = _fresh_audit_logger()
        for _ in range(10):
            _log(al, SeverityLevel.INFO)
        cmd = _cmd_with_logger(al)

        captured_limit = []
        original_get = al.get_recent_events

        def _spy(limit=100, **kwargs):
            captured_limit.append(limit)
            return original_get(limit=limit, **kwargs)

        al.get_recent_events = _spy  # type: ignore[method-assign]

        await cmd.execute(_ctx("recent 999"))
        assert captured_limit, "get_recent_events was not called"
        assert captured_limit[0] <= MAX_SAFE_LIMIT, (
            f"Limit {captured_limit[0]} exceeds MAX_SAFE_LIMIT={MAX_SAFE_LIMIT}"
        )

    async def test_request_below_limit_is_not_changed(self):
        al = _fresh_audit_logger()
        cmd = _cmd_with_logger(al)
        captured_limit = []
        original_get = al.get_recent_events

        def _spy(limit=100, **kwargs):
            captured_limit.append(limit)
            return original_get(limit=limit, **kwargs)

        al.get_recent_events = _spy  # type: ignore[method-assign]

        await cmd.execute(_ctx("recent 10"))
        assert captured_limit[0] == 10


# ---------------------------------------------------------------------------
# Test: recent shows warning when capped
# ---------------------------------------------------------------------------


class TestRecentShowsWarningWhenCapped:
    async def test_warning_shown_when_limit_exceeds_max(self):
        al = _fresh_audit_logger()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx(f"recent {MAX_SAFE_LIMIT + 1}"))
        rendered = _render(result.content)
        assert any(word in rendered.lower() for word in ("aviso", "reduzido", "limite")), (
            f"Expected warning text in output when limit exceeds MAX_SAFE_LIMIT: {rendered[:400]}"
        )

    async def test_no_warning_when_limit_within_max(self):
        al = _fresh_audit_logger()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("recent 50"))
        rendered = _render(result.content)
        assert "reduzido automaticamente" not in rendered.lower()


# ---------------------------------------------------------------------------
# Test: overview no crash with zero events
# ---------------------------------------------------------------------------


class TestOverviewNoCrashWithZeroEvents:
    async def test_zero_events_does_not_crash(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx(""))
        assert result.success is True

    async def test_zero_events_renders_without_repr_artifacts(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx(""))
        rendered = _render(result.content)
        assert "<rich." not in rendered
        assert rendered.strip()

    async def test_zero_events_shows_zero_total(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx(""))
        rendered = _render(result.content)
        assert "0" in rendered


# ---------------------------------------------------------------------------
# Test: overview no crash when get_security_summary returns string
# ---------------------------------------------------------------------------


class TestOverviewNoCrashWhenSummaryIsString:
    async def test_string_summary_does_not_crash(self):
        al = _fresh_audit_logger()
        al.get_security_summary = lambda: "unexpected string"  # type: ignore[method-assign]
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx(""))
        assert result.success is True

    async def test_string_summary_renders_without_key_error(self):
        al = _fresh_audit_logger()
        al.get_security_summary = lambda: "unexpected string"  # type: ignore[method-assign]
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx(""))
        rendered = _render(result.content)
        assert "<rich." not in rendered


# ---------------------------------------------------------------------------
# Test: errors default shows all severities
# ---------------------------------------------------------------------------


class TestErrorsDefaultShowsAllSeverities:
    async def test_default_shows_warning(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        _log(al, SeverityLevel.WARNING)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("errors"))
        assert result.success is True
        rendered = _render(result.content)
        assert "AVISO" in rendered or "WARNING" in rendered.upper() or "aviso" in rendered.lower()

    async def test_default_shows_error(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        _log(al, SeverityLevel.ERROR)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("errors"))
        assert result.success is True
        rendered = _render(result.content)
        assert "ERRO" in rendered or "ERROR" in rendered.upper() or "erro" in rendered.lower()

    async def test_default_shows_critical(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        _log(al, SeverityLevel.CRITICAL)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("errors"))
        assert result.success is True
        rendered = _render(result.content)
        assert "CRÍT" in rendered or "CRITICAL" in rendered.upper() or "crít" in rendered.lower()


# ---------------------------------------------------------------------------
# Test: errors filtered by severity
# ---------------------------------------------------------------------------


class TestErrorsFilteredBySeverity:
    async def _setup_all_severities(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        _log(al, SeverityLevel.WARNING)
        _log(al, SeverityLevel.ERROR)
        _log(al, SeverityLevel.CRITICAL)
        return al

    async def test_filtered_by_warning_only(self):
        al = await self._setup_all_severities()
        cmd = _cmd_with_logger(al)
        captured: list = []
        original = al.get_recent_events

        def _spy(limit=100, severity=None, **kwargs):
            captured.append(severity)
            return original(limit=limit, severity=severity, **kwargs)

        al.get_recent_events = _spy  # type: ignore[method-assign]

        result = await cmd.execute(_ctx("errors --severity warning"))
        assert result.success is True
        assert SeverityLevel.WARNING in captured
        assert SeverityLevel.ERROR not in captured
        assert SeverityLevel.CRITICAL not in captured

    async def test_filtered_by_error_only(self):
        al = await self._setup_all_severities()
        cmd = _cmd_with_logger(al)
        captured: list = []
        original = al.get_recent_events

        def _spy(limit=100, severity=None, **kwargs):
            captured.append(severity)
            return original(limit=limit, severity=severity, **kwargs)

        al.get_recent_events = _spy  # type: ignore[method-assign]

        result = await cmd.execute(_ctx("errors --severity error"))
        assert result.success is True
        assert SeverityLevel.ERROR in captured
        assert SeverityLevel.WARNING not in captured
        assert SeverityLevel.CRITICAL not in captured

    async def test_filtered_by_critical_only(self):
        al = await self._setup_all_severities()
        cmd = _cmd_with_logger(al)
        captured: list = []
        original = al.get_recent_events

        def _spy(limit=100, severity=None, **kwargs):
            captured.append(severity)
            return original(limit=limit, severity=severity, **kwargs)

        al.get_recent_events = _spy  # type: ignore[method-assign]

        result = await cmd.execute(_ctx("errors --severity critical"))
        assert result.success is True
        assert SeverityLevel.CRITICAL in captured
        assert SeverityLevel.WARNING not in captured
        assert SeverityLevel.ERROR not in captured

    async def test_unknown_severity_falls_back_to_all(self):
        al = await self._setup_all_severities()
        cmd = _cmd_with_logger(al)
        captured: list = []
        original = al.get_recent_events

        def _spy(limit=100, severity=None, **kwargs):
            captured.append(severity)
            return original(limit=limit, severity=severity, **kwargs)

        al.get_recent_events = _spy  # type: ignore[method-assign]

        result = await cmd.execute(_ctx("errors --severity unknown_level"))
        assert result.success is True
        assert SeverityLevel.WARNING in captured
        assert SeverityLevel.ERROR in captured
        assert SeverityLevel.CRITICAL in captured


# ---------------------------------------------------------------------------
# Test: all UI strings in Portuguese
# ---------------------------------------------------------------------------


_ENGLISH_FORBIDDEN = [
    "Total Events",
    "Session ID",
    "Permission Denials",
    "Secret Detections",
    "Critical Events",
    "Log File",
    "Recent Activity",
    "Event Types",
    "Quick Commands",
    "No log events found",
    "No recent activity",
    "No errors or warnings found",
    "In-Memory Logs Cleared",
    "Cleared",
    "events from memory",
    "Persistent logs",
    "Logs Exported Successfully",
    "Export Complete",
    "Access control validation",
    "Access denied events",
    "Tool run events",
    "Plan workflow events",
    "Manual approval needed",
    "No events recorded yet",
    "Permission Statistics",
    "Total Checks",
    "Denial Rate",
    "Permission Events",
    "No secret detection events found",
    "No sensitive data detected",
    "No tool execution events found",
    "No plan execution events found",
    "Detailed Audit Statistics",
    "Errors & Warnings",
]


class TestAllUIStringsInPortuguese:
    async def test_overview_has_no_english_strings(self):
        al = _fresh_audit_logger()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx(""))
        rendered = _render(result.content)
        found = [s for s in _ENGLISH_FORBIDDEN if s in rendered]
        assert not found, f"English strings found in /logs overview: {found}"

    async def test_clear_has_no_english_strings(self):
        al = _fresh_audit_logger()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("clear"))
        rendered = _render(result.content)
        found = [s for s in _ENGLISH_FORBIDDEN if s in rendered]
        assert not found, f"English strings found in /logs clear: {found}"

    async def test_errors_empty_has_no_english_strings(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("errors"))
        rendered = _render(result.content)
        found = [s for s in _ENGLISH_FORBIDDEN if s in rendered]
        assert not found, f"English strings found in /logs errors (empty): {found}"

    async def test_recent_empty_has_no_english_strings(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("recent"))
        rendered = _render(result.content)
        found = [s for s in _ENGLISH_FORBIDDEN if s in rendered]
        assert not found, f"English strings found in /logs recent (empty): {found}"

    async def test_get_help_is_in_portuguese(self):
        cmd = LogsCommand()
        help_text = cmd.get_help()
        assert "Visualizar" in help_text or "Uso:" in help_text
        assert "Usage:" not in help_text


# ---------------------------------------------------------------------------
# Test: AuditLogger.clear_events() public method
# ---------------------------------------------------------------------------


class TestAuditLoggerClearEvents:
    def test_clear_events_returns_count(self):
        al = _fresh_audit_logger()
        before = len(al.recent_events)
        _log(al, SeverityLevel.INFO)
        _log(al, SeverityLevel.INFO)
        count = al.clear_events()
        assert count == before + 2

    def test_clear_events_empties_list(self):
        al = _fresh_audit_logger()
        _log(al, SeverityLevel.INFO)
        al.clear_events()
        assert al.recent_events == []

    def test_clear_events_returns_zero_when_already_empty(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        count = al.clear_events()
        assert count == 0


# ---------------------------------------------------------------------------
# Test: AuditLogger.get_security_summary() with zero events
# ---------------------------------------------------------------------------


class TestGetSecuritySummaryZeroEvents:
    def test_zero_events_returns_dict(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        summary = al.get_security_summary()
        assert isinstance(summary, dict)

    def test_zero_events_has_required_keys(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        summary = al.get_security_summary()
        required = {"total_events", "session_id", "event_types", "permission_denials",
                    "secret_detections", "recent_critical_events", "log_file"}
        missing = required - summary.keys()
        assert not missing, f"Missing keys in zero-events summary: {missing}"

    def test_zero_events_totals_are_zero(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        summary = al.get_security_summary()
        assert summary["total_events"] == 0
        assert summary["permission_denials"] == 0
        assert summary["secret_detections"] == 0
        assert summary["recent_critical_events"] == 0


# ---------------------------------------------------------------------------
# Test: basic command execution (smoke)
# ---------------------------------------------------------------------------


class TestLogsCommandSmoke:
    async def test_no_args_returns_success(self):
        result = await LogsCommand().execute(_ctx(""))
        assert result.success is True

    async def test_recent_returns_success(self):
        result = await LogsCommand().execute(_ctx("recent 10"))
        assert result.success is True

    async def test_security_returns_success(self):
        result = await LogsCommand().execute(_ctx("security"))
        assert result.success is True

    async def test_permissions_returns_success(self):
        result = await LogsCommand().execute(_ctx("permissions"))
        assert result.success is True

    async def test_secrets_returns_success(self):
        result = await LogsCommand().execute(_ctx("secrets"))
        assert result.success is True

    async def test_tools_returns_success(self):
        result = await LogsCommand().execute(_ctx("tools"))
        assert result.success is True

    async def test_plans_returns_success(self):
        result = await LogsCommand().execute(_ctx("plans"))
        assert result.success is True

    async def test_errors_returns_success(self):
        result = await LogsCommand().execute(_ctx("errors"))
        assert result.success is True

    async def test_summary_returns_success(self):
        result = await LogsCommand().execute(_ctx("summary"))
        assert result.success is True

    async def test_clear_returns_success(self):
        result = await LogsCommand().execute(_ctx("clear"))
        assert result.success is True

    async def test_unknown_action_raises_command_error(self):
        from deile.core.exceptions import CommandError
        with pytest.raises(CommandError):
            await LogsCommand().execute(_ctx("nonexistent_xyz"))

    async def test_content_type_is_rich(self):
        result = await LogsCommand().execute(_ctx(""))
        assert result.content_type == "rich"

    async def test_renders_without_repr_artifacts(self):
        result = await LogsCommand().execute(_ctx(""))
        rendered = _render(result.content)
        assert "<rich." not in rendered

    async def test_get_help_returns_string(self):
        help_text = LogsCommand().get_help()
        assert isinstance(help_text, str)
        assert len(help_text) > 50

    async def test_export_no_filename_raises_command_error(self):
        from deile.core.exceptions import CommandError
        with pytest.raises(CommandError, match="requer nome de arquivo"):
            await LogsCommand().execute(_ctx("export"))


# ---------------------------------------------------------------------------
# Test: non-empty paths for each subcommand
# ---------------------------------------------------------------------------


class TestNonEmptyPaths:
    async def test_security_logs_with_security_event(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        _log(al, SeverityLevel.WARNING, AuditEventType.PERMISSION_DENIED)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("security"))
        assert result.success is True
        rendered = _render(result.content)
        assert "test_actor" in rendered or "Segurança" in rendered

    async def test_security_logs_with_secret_detected(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        al.log_secret_detection("myfile.py", "api_key", 42, 0.99, redacted=False)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("security"))
        assert result.success is True

    async def test_security_logs_with_secret_redacted(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        al.log_secret_detection("myfile.py", "token", 10, 0.95, redacted=True)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("security"))
        assert result.success is True

    async def test_permission_logs_with_events(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        al.log_permission_check("my_tool", "/etc/passwd", "read", allowed=True)
        al.log_permission_check("my_tool", "/etc/shadow", "read", allowed=False)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("permissions"))
        assert result.success is True
        rendered = _render(result.content)
        assert "Permissão" in rendered or "PERMIT" in rendered or "NEGADO" in rendered

    async def test_secret_logs_with_events(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        al.log_secret_detection("config.py", "password", 5, 0.88, redacted=True)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("secrets"))
        assert result.success is True
        rendered = _render(result.content)
        assert "config" in rendered or "Segredo" in rendered

    async def test_tool_logs_empty(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("tools"))
        assert result.success is True
        rendered = _render(result.content)
        assert "Nenhum evento" in rendered or "ferramenta" in rendered.lower()

    async def test_tool_logs_with_events(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        al.log_tool_execution("bash_tool", "ls /tmp", True, duration_ms=42, exit_code=0)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("tools"))
        assert result.success is True
        rendered = _render(result.content)
        assert "bash_tool" in rendered or "Ferramenta" in rendered

    async def test_plan_logs_with_events(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        al.log_plan_execution("plan-abc", "start", "success", step_count=3)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("plans"))
        assert result.success is True
        rendered = _render(result.content)
        assert "plan-abc" in rendered or "Plano" in rendered

    async def test_plan_logs_with_approval_events(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        al.log_approval_event("plan-xyz", "step-1", "required", "bash_tool", "high")
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("plans"))
        assert result.success is True

    async def test_summary_with_events(self):
        al = _fresh_audit_logger()
        _log(al, SeverityLevel.INFO)
        _log(al, SeverityLevel.WARNING)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx("summary"))
        assert result.success is True
        rendered = _render(result.content)
        assert "%" in rendered

    async def test_export_json_success(self):
        import os
        import tempfile
        al = _fresh_audit_logger()
        cmd = _cmd_with_logger(al)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            result = await cmd.execute(_ctx(f"export {path}"))
            assert result.success is True
            rendered = _render(result.content)
            assert "Exportad" in rendered
        finally:
            if os.path.exists(path):
                os.unlink(path)

    async def test_export_csv_success(self):
        import os
        import tempfile
        al = _fresh_audit_logger()
        cmd = _cmd_with_logger(al)
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            result = await cmd.execute(_ctx(f"export {path} csv"))
            assert result.success is True
        finally:
            if os.path.exists(path):
                os.unlink(path)

    async def test_export_failure_raises_command_error(self):
        from deile.core.exceptions import CommandError
        al = _fresh_audit_logger()

        def _bad_export(*args, **kwargs):
            raise OSError("disk full")

        al.export_audit_log = _bad_export
        cmd = _cmd_with_logger(al)
        with pytest.raises(CommandError, match="Falha ao exportar"):
            await cmd.execute(_ctx("export /tmp/test_export.json"))

    async def test_recent_capped_with_events(self):
        al = _fresh_audit_logger()
        al.recent_events.clear()
        for _ in range(5):
            _log(al, SeverityLevel.INFO)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx(f"recent {MAX_SAFE_LIMIT + 100}"))
        assert result.success is True
        rendered = _render(result.content)
        assert "reduzido" in rendered.lower() or "limite" in rendered.lower()

    async def test_overview_with_events_renders_activity(self):
        al = _fresh_audit_logger()
        _log(al, SeverityLevel.INFO, AuditEventType.TOOL_EXECUTION)
        cmd = _cmd_with_logger(al)
        result = await cmd.execute(_ctx(""))
        assert result.success is True
        rendered = _render(result.content)
        assert rendered.strip()
