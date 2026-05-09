"""Tests for the issue #125 PR-#135 review fixes (P0/P1/P2/S).

This module proves each reviewer finding is closed. The numbering matches
the brief delivered to FIX-IT.

  - P0-1: validation_failed never leaks the raw value (logger or audit).
  - P0-2: ``set_preference`` is gated/audited like ``set_setting``.
  - P1-1: ``_set_typed`` refuses non-list values for list attributes.
  - P1-2: secret refusal emits an audit event.
  - P1-4: ``Settings.load_from_file`` applies converters (not just key allowlist).
  - P1-5: default settings-write rule is fail-closed (READ).
  - P2-1: permission check happens BEFORE dry-run validation.
  - P2-2: trust allowlist comparison is case-insensitive (normcase).
  - P2-3: ``add_skills_path_detailed`` distinguishes denial from no-op.
  - P2-4: ``add_skills_path`` does not create the global dir for project scope.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deile.commands.settings_manager import SettingsManager
from deile.config.settings import (LogLevel, Settings,
                                   _is_project_layer_trusted, _set_typed)
from deile.security.permissions import PermissionLevel, PermissionManager

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# Fixtures — ``allow_writes`` is an alias for the centralized
# ``allow_settings_writes`` (root conftest). The shorter local name is kept
# for diff-friendliness with the existing review-fix assertions.
# ---------------------------------------------------------------------------


@pytest.fixture
def allow_writes(allow_settings_writes):
    return allow_settings_writes


def _make_manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(
        project_dir=tmp_path / "project",
        user_home=tmp_path / "home",
    )


# ---------------------------------------------------------------------------
# P0-1: no value leak in logger or audit on validation failure
# ---------------------------------------------------------------------------


class TestP0_1_NoValueLeakOnValidation:
    def test_audit_error_string_does_not_contain_raw_value(
        self, tmp_path, allow_writes
    ):
        """The converter for ``logging.level`` echoes back the value in
        its ``ValueError`` text. Sanitization must replace it before the
        audit is emitted."""
        mgr = _make_manager(tmp_path)
        secret_like_value = "PRETEND_SECRET_TOKEN_123"

        with patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            result = mgr.set_setting(
                "logging.level", secret_like_value, scope="global"
            )

        assert result is False
        assert audit_mock.called
        details = audit_mock.call_args.kwargs["details"]
        # The raw "value" must NOT appear anywhere in the audit payload.
        flat = json.dumps(details, default=str)
        assert secret_like_value not in flat
        assert "<value>" in details.get("error", "") or details["error"]

    def test_logger_does_not_emit_raw_value(self, tmp_path, allow_writes, caplog):
        mgr = _make_manager(tmp_path)
        secret_like_value = "ANOTHER_PRETEND_SECRET_456"

        with caplog.at_level("ERROR", logger="deile.commands.settings_manager"):
            mgr.set_setting("logging.level", secret_like_value, scope="global")

        for record in caplog.records:
            assert secret_like_value not in record.getMessage()


# ---------------------------------------------------------------------------
# P0-2: set_preference is gated and audited
# ---------------------------------------------------------------------------


class TestP0_2_SetPreferenceGated:
    def test_secret_key_refused_with_audit(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            result = mgr.set_preference("api_key", "sk-secret", scope="global")
        assert result is False
        assert not mgr.global_settings_path.exists()
        audit_mock.assert_called_once()
        kwargs = audit_mock.call_args.kwargs
        assert kwargs["result"] == "refused_secret"
        assert kwargs["details"]["reason"] == "secret_pattern"

    def test_permission_denial_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        pm_mock = MagicMock()
        pm_mock.check_permission.return_value = False
        with patch(
            "deile.commands._settings_security_hooks.get_permission_manager",
            return_value=pm_mock,
        ):
            result = mgr.set_preference("model", "claude-4", scope="global")
        assert result is False
        assert not mgr.global_settings_path.exists()

    def test_success_emits_audit_with_fingerprints(self, tmp_path, allow_writes):
        mgr = _make_manager(tmp_path)
        with patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            assert mgr.set_preference("model", "claude-4", scope="global") is True
        audit_mock.assert_called_once()
        kwargs = audit_mock.call_args.kwargs
        assert kwargs["result"] == "allowed"
        assert kwargs["details"]["new_value_fingerprint"] != "<absent>"


# ---------------------------------------------------------------------------
# P1-1: _set_typed refuses non-list for list attrs
# ---------------------------------------------------------------------------


class TestP1_1_StrictListCoercion:
    def test_string_for_list_attr_is_rejected(self):
        s = Settings()
        s.trust_project_layer_dirs = []  # known good
        _set_typed(s, "trust_project_layer_dirs", "/single/path")
        # Must not have been clobbered with a string.
        assert s.trust_project_layer_dirs == []

    def test_list_for_list_attr_is_accepted(self):
        s = Settings()
        s.trust_project_layer_dirs = []
        _set_typed(s, "trust_project_layer_dirs", ["/a", "/b"])
        assert s.trust_project_layer_dirs == ["/a", "/b"]

    def test_ambiguous_bool_string_rejected(self):
        s = Settings()
        s.enable_file_safety_checks = True
        _set_typed(s, "enable_file_safety_checks", "yes-please")
        # Must NOT silently turn into a string.
        assert s.enable_file_safety_checks is True
        assert isinstance(s.enable_file_safety_checks, bool)


# ---------------------------------------------------------------------------
# P1-2: secret refusal emits audit
# ---------------------------------------------------------------------------


class TestP1_2_SecretRefusalAudit:
    def test_set_setting_secret_refusal_emits_audit(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            result = mgr.set_setting("openai_api_key", "sk-x", scope="global")
        assert result is False
        audit_mock.assert_called_once()
        kwargs = audit_mock.call_args.kwargs
        assert kwargs["result"] == "refused_secret"
        # The raw value must NOT be in the payload.
        assert "sk-x" not in json.dumps(kwargs["details"], default=str)


# ---------------------------------------------------------------------------
# P1-4: load_from_file applies converters
# ---------------------------------------------------------------------------


class TestP1_4_LegacyLoadConverters:
    def test_string_for_list_field_is_dropped(self, tmp_path):
        legacy = tmp_path / "settings.json"
        legacy.write_text(
            json.dumps(
                {
                    "trust_project_layer_dirs": "/single",  # WRONG type
                    "log_level": "INFO",  # OK
                }
            ),
            encoding="utf-8",
        )
        s = Settings.load_from_file(legacy)
        # The bad value must NOT have landed.
        assert s.trust_project_layer_dirs == []
        # The good value did land.
        assert s.log_level == LogLevel.INFO

    def test_invalid_bool_string_is_dropped(self, tmp_path):
        legacy = tmp_path / "settings.json"
        legacy.write_text(
            json.dumps({"enable_file_safety_checks": "yes-please"}),
            encoding="utf-8",
        )
        s = Settings.load_from_file(legacy)
        # Must keep the dataclass default (True), not a stray string.
        assert s.enable_file_safety_checks is True

    def test_unknown_keys_still_dropped(self, tmp_path):
        legacy = tmp_path / "settings.json"
        legacy.write_text(
            json.dumps({"working_directory": "/etc"}),  # NOT in handlers
            encoding="utf-8",
        )
        s = Settings.load_from_file(legacy)
        # Default working_directory must NOT have been set to /etc.
        assert str(s.working_directory) != "/etc"


# ---------------------------------------------------------------------------
# P1-5: fail-closed default
# ---------------------------------------------------------------------------


class TestP1_5_FailClosedDefault:
    def test_default_rule_denies_settings_write(self):
        # Build a *fresh* manager with default rules (avoid the session
        # singleton mutated by other fixtures).
        pm = PermissionManager()
        rule = pm.get_rule_by_id("settings_write_default")
        assert rule is not None
        # The default permission level must be READ (= no write).
        assert rule.permission_level == PermissionLevel.READ
        # And check_permission must reflect that.
        allowed = pm.check_permission(
            tool_name="settings_manager",
            resource="settings:global:logging.level",
            action="write",
        )
        assert allowed is False


# ---------------------------------------------------------------------------
# P2-1: permission check before validation
# ---------------------------------------------------------------------------


class TestP2_1_OrderingPermBeforeValidation:
    def test_denied_caller_cannot_probe_validator(self, tmp_path):
        """A caller without permission must see ``denied`` regardless of
        whether the value would have failed validation. Otherwise the
        difference between ``denied`` and ``invalid`` leaks the validator
        surface."""
        mgr = _make_manager(tmp_path)
        pm_mock = MagicMock()
        pm_mock.check_permission.return_value = False

        with patch(
            "deile.commands._settings_security_hooks.get_permission_manager",
            return_value=pm_mock,
        ), patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            # Both invalid (int) and would-be-valid ("INFO") must yield denied.
            mgr.set_setting("logging.level", 42, scope="global")
            mgr.set_setting("logging.level", "INFO", scope="global")

        assert audit_mock.call_count == 2
        for call in audit_mock.call_args_list:
            assert call.kwargs["result"] == "denied"


# ---------------------------------------------------------------------------
# P2-2: case-insensitive path comparison
# ---------------------------------------------------------------------------


class TestP2_2_CaseInsensitivePaths:
    def test_normcase_matches_mixed_case(self, tmp_path):
        # Build a real directory and put a mixed-case version on the
        # allowlist. On case-insensitive filesystems they refer to the
        # same path; on case-sensitive ones they don't and the test still
        # passes thanks to normcase being identity there.
        target = tmp_path / "Project"
        target.mkdir()
        # Permute case
        case_variant = str(target).swapcase()

        if os.path.normcase(str(target)) == os.path.normcase(case_variant):
            # We're on a case-insensitive FS — the trust check must accept.
            trusted, reason = _is_project_layer_trusted(
                target, [case_variant], "deny"
            )
            assert trusted is True
            assert reason == "allowlisted"
        else:
            pytest.skip("Case-sensitive filesystem — variant is a different dir")

    def test_non_list_allowlist_is_ignored(self):
        """Defense-in-depth: if a string somehow reaches
        ``_is_project_layer_trusted`` (despite the new ``_set_typed``
        guard), it must be treated as empty rather than iterated."""
        trusted, reason = _is_project_layer_trusted(
            Path("/tmp/x"), "/tmp/x", "deny"  # type: ignore[arg-type]
        )
        # No allowlisting because the string was rejected.
        assert trusted is False
        assert reason == "denied_by_policy"


# ---------------------------------------------------------------------------
# P2-3: add_skills_path_detailed reasons
# ---------------------------------------------------------------------------


class TestP2_3_DetailedSkillsResult:
    def test_reasons_added_already_present_denied(self, tmp_path, allow_writes):
        mgr = _make_manager(tmp_path)
        target = tmp_path / "skill-dir"
        target.mkdir()

        ok, reason = mgr.add_skills_path_detailed(target, scope="global")
        assert ok is True and reason == "added"

        ok, reason = mgr.add_skills_path_detailed(target, scope="global")
        assert ok is False and reason == "already_present"

    def test_reason_denied(self, tmp_path):
        mgr = _make_manager(tmp_path)
        pm_mock = MagicMock()
        pm_mock.check_permission.return_value = False
        with patch(
            "deile.commands._settings_security_hooks.get_permission_manager",
            return_value=pm_mock,
        ):
            ok, reason = mgr.add_skills_path_detailed(
                tmp_path / "x", scope="global"
            )
        assert ok is False and reason == "denied"

    def test_remove_reasons(self, tmp_path, allow_writes):
        mgr = _make_manager(tmp_path)
        target = "/some/skill"
        mgr.global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.global_settings_path.write_text(
            json.dumps({"skills_paths": [target]}), encoding="utf-8"
        )

        ok, reason = mgr.remove_skills_path_detailed(target, scope="global")
        assert ok is True and reason == "removed"

        ok, reason = mgr.remove_skills_path_detailed(target, scope="global")
        assert ok is False and reason == "not_found"


# ---------------------------------------------------------------------------
# P2-4: project-scope add does not create global dir
# ---------------------------------------------------------------------------


class TestP2_4_ProjectScopeNoGlobalDirCreation:
    def test_project_scope_does_not_touch_global_dir(self, tmp_path, allow_writes):
        # Use a clearly distinct user_home so we can assert it stays absent.
        home = tmp_path / "home"
        project = tmp_path / "project"
        project.mkdir()
        mgr = SettingsManager(project_dir=project, user_home=home)

        target = tmp_path / "skill-dir"
        target.mkdir()
        assert mgr.add_skills_path(target, scope="project") is True

        # The global ~/.deile dir must NOT have been created.
        assert not (home / ".deile").exists()
        # The project dir was written.
        assert (project / ".deile" / "settings.json").exists()
