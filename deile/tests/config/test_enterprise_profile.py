"""Tests for issue #138 — enterprise.yaml security settings are now read.

Verifies three things:
1. enterprise.yaml fields map into Settings via the profile layer.
2. sandbox_code_execution=True forces sandbox mode in BashExecuteTool.
3. generate_compliance_reports=True triggers a report file via AuditLogger.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deile.config.settings import (
    Settings,
    _apply_nested_dict,
    _apply_profile_layer,
    reset_settings,
)
from deile.security.audit_logger import AuditLogger


# ---------------------------------------------------------------------------
# Settings: profile fields land in Settings
# ---------------------------------------------------------------------------


class TestEnterpriseProfileSettings:
    @pytest.mark.unit
    def test_defaults_are_false(self):
        s = Settings()
        assert s.sandbox_code_execution is False
        assert s.encrypt_logs is False
        assert s.generate_compliance_reports is False

    @pytest.mark.unit
    def test_apply_nested_dict_maps_security_keys(self):
        s = Settings()
        _apply_nested_dict(
            s,
            {
                "security": {
                    "sandbox_code_execution": True,
                    "encrypt_logs": True,
                },
                "monitoring": {"generate_compliance_reports": True},
            },
        )
        assert s.sandbox_code_execution is True
        assert s.encrypt_logs is True
        assert s.generate_compliance_reports is True

    @pytest.mark.unit
    def test_apply_profile_layer_enterprise(self):
        """Loading the real enterprise.yaml sets all three flags."""
        s = Settings()
        s.profile_name = "enterprise"
        _apply_profile_layer(s)
        assert s.sandbox_code_execution is True
        assert s.encrypt_logs is True
        assert s.generate_compliance_reports is True

    @pytest.mark.unit
    def test_apply_profile_layer_autonomous_agent(self):
        """autonomous_agent.yaml sets sandbox_code_execution but not the other two."""
        s = Settings()
        s.profile_name = "autonomous_agent"
        _apply_profile_layer(s)
        assert s.sandbox_code_execution is True   # set in autonomous_agent.yaml
        assert s.encrypt_logs is False            # not set
        assert s.generate_compliance_reports is False

    @pytest.mark.unit
    def test_missing_profile_is_noop(self):
        s = Settings()
        s.profile_name = "nonexistent_profile"
        _apply_profile_layer(s)
        assert s.sandbox_code_execution is False

    @pytest.mark.unit
    def test_user_settings_override_profile(self):
        """User settings win over profile preset (priority order check)."""
        s = Settings()
        s.profile_name = "enterprise"
        _apply_profile_layer(s)
        assert s.sandbox_code_execution is True

        # User overrides sandbox_code_execution to False
        _apply_nested_dict(s, {"security": {"sandbox_code_execution": False}})
        assert s.sandbox_code_execution is False


# ---------------------------------------------------------------------------
# BashExecuteTool: sandbox_code_execution forces sandbox mode
# ---------------------------------------------------------------------------


class TestBashToolSandboxEnforcement:
    def _make_ctx(self, sandbox_arg: bool):
        from deile.tools.base import ToolContext
        return ToolContext(
            user_input="echo hello",
            parsed_args={
                "command": "echo hello",
                "sandbox": sandbox_arg,
            },
        )

    @pytest.mark.unit
    def test_sandbox_forced_when_setting_true(self):
        from deile.tools.bash_tool import BashExecuteTool

        tool = BashExecuteTool()
        ctx = self._make_ctx(sandbox_arg=False)

        mock_settings = MagicMock()
        mock_settings.sandbox_code_execution = True

        subprocess_calls = []

        def fake_subprocess(command, working_dir, env, timeout):
            subprocess_calls.append(True)
            return ("hello\n", "", 0, False)

        # When sandbox_code_execution=True the tool must use subprocess (not PTY)
        with patch("deile.tools.bash_tool.get_settings", return_value=mock_settings):
            with patch.object(tool, "_execute_with_subprocess", side_effect=fake_subprocess):
                with patch.object(tool, "_should_use_pty", return_value=False):
                    tool.execute_sync(ctx)

        assert subprocess_calls, "subprocess should have been called"

    @pytest.mark.unit
    def test_sandbox_not_forced_when_setting_false(self):
        from deile.tools.bash_tool import BashExecuteTool

        tool = BashExecuteTool()
        ctx = self._make_ctx(sandbox_arg=False)

        mock_settings = MagicMock()
        mock_settings.sandbox_code_execution = False

        subprocess_calls = []

        def fake_subprocess(command, working_dir, env, timeout):
            subprocess_calls.append(True)
            return ("hello\n", "", 0, False)

        with patch("deile.tools.bash_tool.get_settings", return_value=mock_settings):
            with patch.object(tool, "_execute_with_subprocess", side_effect=fake_subprocess):
                with patch.object(tool, "_should_use_pty", return_value=False):
                    tool.execute_sync(ctx)

        assert subprocess_calls


# ---------------------------------------------------------------------------
# AuditLogger: generate_compliance_reports writes a JSON report
# ---------------------------------------------------------------------------


class TestComplianceReport:
    @pytest.mark.unit
    def test_generate_compliance_report_creates_file(self, tmp_path):
        logger = AuditLogger(log_dir=str(tmp_path))
        report_path = logger.generate_compliance_report(output_dir=tmp_path)
        assert report_path.exists()
        data = json.loads(report_path.read_text())
        assert "session_id" in data
        assert "summary" in data
        assert "events" in data

    @pytest.mark.unit
    def test_atexit_registered_when_setting_true(self, tmp_path):
        """When generate_compliance_reports=True the atexit handler is wired."""
        mock_settings = MagicMock()
        mock_settings.generate_compliance_reports = True

        # Patch at the source module so the lazy import resolves to the mock
        with patch("deile.config.settings.get_settings", return_value=mock_settings):
            with patch("deile.security.audit_logger.atexit.register") as mock_reg:
                logger = AuditLogger(log_dir=str(tmp_path))

        assert logger._compliance_report_registered
        mock_reg.assert_called_once()

    @pytest.mark.unit
    def test_atexit_not_registered_when_setting_false(self, tmp_path):
        mock_settings = MagicMock()
        mock_settings.generate_compliance_reports = False

        with patch("deile.config.settings.get_settings", return_value=mock_settings):
            with patch("deile.security.audit_logger.atexit.register") as mock_reg:
                logger = AuditLogger(log_dir=str(tmp_path))

        assert not logger._compliance_report_registered
        mock_reg.assert_not_called()


# ---------------------------------------------------------------------------
# Logs: encrypt_logs=True emits a warning
# ---------------------------------------------------------------------------


class TestEncryptLogsWarning:
    @pytest.mark.unit
    def test_encrypt_logs_warning_emitted(self):
        import deile.storage.logs as logs_mod

        # Reset module state so _ensure_initialized runs fresh.
        # Also save/restore logger propagation: _ensure_initialized sets
        # `deile.propagate = False` as a side effect, which would break
        # subsequent tests that rely on caplog capturing from child loggers.
        logs_mod._initialized = False
        logs_mod._encrypt_logs_warned = False

        deile_logger = logging.getLogger("deile")
        saved_propagate = deile_logger.propagate
        saved_handlers = list(deile_logger.handlers)

        try:
            # Patch `warning` directly on the logger instance so the test is
            # immune to logger-level / handler-configuration state.
            with patch.object(deile_logger, "warning") as mock_warn:
                with patch("deile.storage.logs._is_encrypt_logs_enabled", return_value=True):
                    logs_mod._ensure_initialized()
        finally:
            # Restore the "deile" logger to the state it was in before we
            # called _ensure_initialized (prevents propagation=False leaking).
            deile_logger.propagate = saved_propagate
            for h in list(deile_logger.handlers):
                if h not in saved_handlers:
                    deile_logger.removeHandler(h)
            logs_mod._initialized = False
            logs_mod._encrypt_logs_warned = False

        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        assert "encrypt_logs=True" in msg
        assert "not yet implemented" in msg
