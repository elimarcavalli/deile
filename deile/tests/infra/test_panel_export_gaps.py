"""Tests: _redact_for_export recursive, _deile_export_dir mode, and exact key-sets (issue #461)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel

from deile.ui.panel.observability.screens import LiveSessionData


class TestRedactForExport:
    """AC3: _redact_for_export — recursive dict/list and None no-op."""

    def test_none_redactor_returns_value_unchanged(self):
        assert panel._redact_for_export("hello", None) == "hello"

    def test_none_redactor_none_value(self):
        assert panel._redact_for_export(None, None) is None

    def test_none_redactor_dict(self):
        d = {"a": "val", "b": 42}
        assert panel._redact_for_export(d, None) == d

    def test_none_redactor_list(self):
        lst = ["x", "y"]
        assert panel._redact_for_export(lst, None) == lst

    def test_string_redacted(self):
        from deile.security.secrets_scanner import SecretsScanner

        redactor = SecretsScanner()
        secret = "ghp_" + "C" * 36
        result = panel._redact_for_export(secret, redactor)
        assert secret not in result

    def test_dict_keys_preserved(self):
        from deile.security.secrets_scanner import SecretsScanner

        redactor = SecretsScanner()
        secret = "ghp_" + "C" * 36
        d = {"key": secret, "other": "safe"}
        result = panel._redact_for_export(d, redactor)
        assert set(result.keys()) == {"key", "other"}

    def test_nested_dict_redacted(self):
        """Recursive: secret inside a nested dict is redacted."""
        from deile.security.secrets_scanner import SecretsScanner

        redactor = SecretsScanner()
        secret = "ghp_" + "C" * 36
        nested = {"outer": {"inner": f"token={secret}"}}
        result = panel._redact_for_export(nested, redactor)
        assert secret not in json.dumps(result)
        assert "outer" in result
        assert "inner" in result["outer"]

    def test_nested_list_redacted(self):
        """Recursive: secret inside a list item is redacted."""
        from deile.security.secrets_scanner import SecretsScanner

        redactor = SecretsScanner()
        secret = "ghp_" + "C" * 36
        lst = [f"token={secret}", "safe"]
        result = panel._redact_for_export(lst, redactor)
        assert secret not in json.dumps(result)
        assert len(result) == 2

    def test_non_string_non_dict_non_list_passthrough(self):
        from deile.security.secrets_scanner import SecretsScanner

        redactor = SecretsScanner()
        assert panel._redact_for_export(42, redactor) == 42
        assert panel._redact_for_export(3.14, redactor) == 3.14
        assert panel._redact_for_export(True, redactor) is True
        assert panel._redact_for_export(None, redactor) is None


class TestDeileExportDir:
    """AC4: _deile_export_dir creates ~/.deile/exports/ with mode 0o700."""

    def test_dir_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = panel._deile_export_dir()
        assert result.exists()
        assert result.is_dir()

    def test_dir_mode_0o700(self, tmp_path, monkeypatch):
        import os

        if os.name == "nt":
            import pytest

            pytest.skip("mode bits not enforced on Windows")
        monkeypatch.setenv("HOME", str(tmp_path))
        result = panel._deile_export_dir()
        mode = oct(result.stat().st_mode & 0o777)
        assert mode == "0o700", f"Expected 0o700, got {mode}"

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        r1 = panel._deile_export_dir()
        r2 = panel._deile_export_dir()
        assert r1 == r2


class TestBuildLiveSessionJsonExactKeys:
    """AC5: _build_live_session_json exact envelope/payload key-sets for v1 and v2."""

    def test_v1_envelope_exact_keys(self):
        data = LiveSessionData(session=None, command=None, chat=None, api_errors=[])
        obj = panel._build_live_session_json(data, [], redactor=None)
        assert set(obj.keys()) == {"schema_version", "kind", "exported_at", "payload"}

    def test_v1_payload_exact_keys(self):
        data = LiveSessionData(session=None, command=None, chat=None, api_errors=[])
        obj = panel._build_live_session_json(data, [], redactor=None)
        assert set(obj["payload"].keys()) == {
            "session",
            "command",
            "chat",
            "api_errors",
            "stdout",
        }

    def test_v2_envelope_exact_keys(self):
        data = LiveSessionData(session=None, command=None, chat=None, api_errors=[])
        obj = panel._build_live_session_json(
            data, [], redactor=None, include_history=True
        )
        assert set(obj.keys()) == {
            "schema_version",
            "kind",
            "exported_at",
            "payload",
            "history",
        }

    def test_v2_payload_exact_keys(self):
        data = LiveSessionData(session=None, command=None, chat=None, api_errors=[])
        obj = panel._build_live_session_json(
            data, [], redactor=None, include_history=True
        )
        assert set(obj["payload"].keys()) == {
            "session",
            "command",
            "chat",
            "api_errors",
            "stdout",
        }

    def test_v1_no_history_key(self):
        data = LiveSessionData(session=None, command=None, chat=None, api_errors=[])
        obj = panel._build_live_session_json(data, [], redactor=None)
        assert "history" not in obj

    def test_v1_schema_version(self):
        data = LiveSessionData(session=None, command=None, chat=None, api_errors=[])
        obj = panel._build_live_session_json(data, [], redactor=None)
        assert obj["schema_version"] == "deile.export.v1"

    def test_v2_schema_version(self):
        data = LiveSessionData(session=None, command=None, chat=None, api_errors=[])
        obj = panel._build_live_session_json(
            data, [], redactor=None, include_history=True
        )
        assert obj["schema_version"] == "deile.export.v2"

    def test_v1_json_roundtrip(self):
        """AC6: v1 result is JSON-serializable and round-trips cleanly."""
        data = LiveSessionData(
            session={"task_id": "t1"}, command=None, chat=None, api_errors=[]
        )
        obj = panel._build_live_session_json(data, [], redactor=None)
        restored = json.loads(json.dumps(obj))
        assert restored["payload"]["session"]["task_id"] == "t1"
