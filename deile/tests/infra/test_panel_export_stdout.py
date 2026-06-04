"""Tests: stdout field in export (AC9/AC10/AC11, issue #547)."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel
from deile.ui.panel.observability.screens import LiveSessionData


class TestSchemaV2WithStdout:
    def test_stdout_present_yields_v2(self):
        data = LiveSessionData(
            session=None, command=None, chat=None, api_errors=[], stdout="some output"
        )
        obj = panel._build_live_session_json(data, [], redactor=None)
        assert obj["schema_version"] == "deile.export.v2"
        assert obj["payload"]["stdout"] == "some output"

    def test_stdout_none_still_v2(self):
        data = LiveSessionData(
            session=None, command=None, chat=None, api_errors=[], stdout=None
        )
        obj = panel._build_live_session_json(data, [], redactor=None)
        assert obj["schema_version"] == "deile.export.v2"
        assert obj["payload"]["stdout"] is None

    def test_stdout_redacted(self):
        from deile.security.secrets_scanner import SecretsScanner
        redactor = SecretsScanner()
        secret = "ghp_" + "B" * 36
        data = LiveSessionData(
            session=None, command=None, chat=None, api_errors=[], stdout=f"token={secret}"
        )
        obj = panel._build_live_session_json(data, [], redactor=redactor)
        import json
        serialized = json.dumps(obj)
        assert secret not in serialized


class TestLiveSessionDataStdout:
    def test_stdout_default_none(self):
        data = LiveSessionData(
            session=None, command=None, chat=None, api_errors=[]
        )
        assert data.stdout is None

    def test_stdout_field_present(self):
        data = LiveSessionData(
            session=None, command=None, chat=None, api_errors=[], stdout="output"
        )
        assert data.stdout == "output"
