"""Tests: SettingsManager permission gate + audit emission (issue #125).

Covers gap A from the #125 hardening work:
  - ``set_setting`` calls PermissionManager and emits SECURITY_POLICY_CHANGED.
  - ``add_skills_path`` / ``remove_skills_path`` likewise.
  - Permission denial returns False and writes nothing.
  - Audit details hash secret-keys to ``"<redacted>"`` and other values to
    SHA-256 truncated digests.
  - Dry-run validation against ``_OVERRIDE_HANDLERS`` rejects type mismatches.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deile.commands.settings_manager import (SettingsManager, _hash_value,
                                             _is_secret_key,
                                             _value_fingerprint)


def _make_manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(
        project_dir=tmp_path / "project",
        user_home=tmp_path / "home",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestSecretKeyHelpers:
    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "openai_api_key",
            "model.api_key",
            "auth.token",
            "secret_value",
            "PASSWORD",
            "session.token",
        ],
    )
    def test_is_secret_key_blocks_known_patterns(self, key):
        assert _is_secret_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "logging.level",
            "ui.streaming_enabled",
            "file_safety.enabled",
            "skills_paths",
        ],
    )
    def test_is_secret_key_allows_neutral_keys(self, key):
        assert _is_secret_key(key) is False

    def test_hash_value_is_deterministic_and_truncated(self):
        h1 = _hash_value({"a": 1})
        h2 = _hash_value({"a": 1})
        assert h1 == h2
        assert len(h1) == 16
        # different value must produce different hash
        assert _hash_value({"a": 2}) != h1

    def test_value_fingerprint_redacts_secret_keys(self):
        assert _value_fingerprint("api_key", "sk-abcdef") == "<redacted>"
        assert _value_fingerprint("auth.token", "xyz") == "<redacted>"

    def test_value_fingerprint_absent_marker_for_none(self):
        assert _value_fingerprint("logging.level", None) == "<absent>"

    def test_value_fingerprint_hashes_non_secret(self):
        fp = _value_fingerprint("logging.level", "INFO")
        assert fp != "<redacted>"
        assert fp != "<absent>"
        assert len(fp) == 16


# ---------------------------------------------------------------------------
# set_setting — permission gate + audit
# ---------------------------------------------------------------------------


class TestSetSettingPermissionAndAudit:
    def test_emits_audit_on_successful_write(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            assert mgr.set_setting("logging.level", "INFO", scope="global") is True
        audit_mock.assert_called_once()
        kwargs = audit_mock.call_args.kwargs
        assert kwargs["scope"] == "global"
        assert kwargs["resource_detail"] == "logging.level"
        assert kwargs["action"] == "write"
        assert kwargs["result"] == "allowed"
        details = kwargs["details"]
        assert details["key_path"] == "logging.level"
        assert details["new_value_fingerprint"] != "<redacted>"
        assert details["old_value_fingerprint"] == "<absent>"

    def test_calls_permission_manager(self, tmp_path):
        mgr = _make_manager(tmp_path)
        pm_mock = MagicMock()
        pm_mock.check_permission.return_value = True
        with patch(
            "deile.security.permissions.get_permission_manager", return_value=pm_mock
        ):
            mgr.set_setting("logging.level", "INFO", scope="global")
        pm_mock.check_permission.assert_called_once()
        kwargs = pm_mock.check_permission.call_args.kwargs
        assert kwargs["tool_name"] == "settings_manager"
        assert kwargs["resource"] == "settings:global:logging.level"
        assert kwargs["action"] == "write"

    def test_permission_denial_returns_false_and_does_not_write(self, tmp_path):
        mgr = _make_manager(tmp_path)
        pm_mock = MagicMock()
        pm_mock.check_permission.return_value = False
        with patch(
            "deile.security.permissions.get_permission_manager", return_value=pm_mock
        ):
            result = mgr.set_setting("logging.level", "INFO", scope="global")
        assert result is False
        # File must not have been created.
        assert not mgr.global_settings_path.exists()

    def test_permission_denial_emits_denied_audit(self, tmp_path):
        mgr = _make_manager(tmp_path)
        pm_mock = MagicMock()
        pm_mock.check_permission.return_value = False
        with patch(
            "deile.security.permissions.get_permission_manager", return_value=pm_mock
        ), patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            mgr.set_setting("logging.level", "INFO", scope="global")
        audit_mock.assert_called_once()
        kwargs = audit_mock.call_args.kwargs
        assert kwargs["result"] == "denied"
        assert kwargs["details"]["reason"] == "permission_denied"

    def test_secret_key_audit_redacts(self, tmp_path):
        """Even though set_setting refuses secret keys, a hypothetical
        future caller bypassing the early-return must still see <redacted>
        in the fingerprint helper."""
        mgr = _make_manager(tmp_path)
        # Direct hit on the helper — set_setting itself short-circuits before
        # audit emission for secret-pattern keys.
        assert _value_fingerprint("openai_api_key", "sk-real-secret") == "<redacted>"
        # And set_setting refuses to even attempt the write:
        assert mgr.set_setting("openai_api_key", "sk-secret", scope="global") is False
        assert not mgr.global_settings_path.exists()

    def test_dry_run_validation_rejects_type_mismatch(self, tmp_path):
        """`logging.level` expects a LogLevel-coercible string. A bare int
        must be rejected before the file is touched."""
        mgr = _make_manager(tmp_path)
        with patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            result = mgr.set_setting("logging.level", 42, scope="global")
        assert result is False
        assert not mgr.global_settings_path.exists()
        audit_mock.assert_called_once()
        kwargs = audit_mock.call_args.kwargs
        assert kwargs["result"] == "invalid"
        assert kwargs["details"]["reason"] == "validation_failed"

    def test_dry_run_validation_accepts_valid_value(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.set_setting("logging.level", "INFO", scope="global") is True

    def test_dry_run_skipped_for_keys_outside_handlers(self, tmp_path):
        """Keys without an `_OVERRIDE_HANDLERS` entry (e.g. ad-hoc nested
        knobs) must still be writable — dry-run is opt-in coverage."""
        mgr = _make_manager(tmp_path)
        assert mgr.set_setting("custom.nested.value", 123, scope="global") is True

    def test_old_value_fingerprint_recorded_on_overwrite(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.set_setting("logging.level", "INFO", scope="global")
        with patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            mgr.set_setting("logging.level", "DEBUG", scope="global")
        details = audit_mock.call_args.kwargs["details"]
        assert details["old_value_fingerprint"] != "<absent>"
        assert details["old_value_fingerprint"] != details["new_value_fingerprint"]


# ---------------------------------------------------------------------------
# add_skills_path / remove_skills_path — permission gate + audit
# ---------------------------------------------------------------------------


class TestSkillsPathPermissionAndAudit:
    def test_add_emits_audit_on_success(self, tmp_path):
        mgr = _make_manager(tmp_path)
        target = tmp_path / "skills_a"
        with patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            assert mgr.add_skills_path(target, scope="global") is True
        audit_mock.assert_called_once()
        kwargs = audit_mock.call_args.kwargs
        assert kwargs["resource_detail"] == "skills_paths"
        assert kwargs["action"] == "add_skills_path"
        assert kwargs["result"] == "allowed"
        assert kwargs["details"]["operation"] == "add"

    def test_add_denial_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        pm_mock = MagicMock()
        pm_mock.check_permission.return_value = False
        with patch(
            "deile.security.permissions.get_permission_manager", return_value=pm_mock
        ):
            assert mgr.add_skills_path(tmp_path / "x", scope="global") is False
        assert not mgr.global_settings_path.exists()

    def test_add_denial_emits_denied_audit(self, tmp_path):
        mgr = _make_manager(tmp_path)
        pm_mock = MagicMock()
        pm_mock.check_permission.return_value = False
        with patch(
            "deile.security.permissions.get_permission_manager", return_value=pm_mock
        ), patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            mgr.add_skills_path(tmp_path / "x", scope="global")
        audit_mock.assert_called_once()
        assert audit_mock.call_args.kwargs["result"] == "denied"

    def test_remove_emits_audit_on_success(self, tmp_path):
        mgr = _make_manager(tmp_path)
        # First add a path (this also emits audit; reset mock by re-patching).
        target = "/some/skill/dir"
        mgr.global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.global_settings_path.write_text(
            json.dumps({"skills_paths": [target]}), encoding="utf-8"
        )
        with patch(
            "deile.commands.settings_manager._emit_settings_audit"
        ) as audit_mock:
            assert mgr.remove_skills_path(target, scope="global") is True
        audit_mock.assert_called_once()
        kwargs = audit_mock.call_args.kwargs
        assert kwargs["action"] == "remove_skills_path"
        assert kwargs["result"] == "allowed"

    def test_remove_denial_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        target = "/some/skill/dir"
        mgr.global_settings_path.parent.mkdir(parents=True, exist_ok=True)
        mgr.global_settings_path.write_text(
            json.dumps({"skills_paths": [target]}), encoding="utf-8"
        )
        pm_mock = MagicMock()
        pm_mock.check_permission.return_value = False
        with patch(
            "deile.security.permissions.get_permission_manager", return_value=pm_mock
        ):
            assert mgr.remove_skills_path(target, scope="global") is False
        # File still has the original content
        data = json.loads(mgr.global_settings_path.read_text(encoding="utf-8"))
        assert data["skills_paths"] == [target]
