"""Tests: plain-text export format for panel export (#547)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel


def _make_session_data(stdout=None):
    """Helper: create a realistic LiveSessionData-like object."""
    from deile.ui.panel.observability.screens import LiveSessionData
    return LiveSessionData(
        session={"task_id": "t1", "stage": "implement", "alive": True},
        command={"cmd": ["python3", "-m", "pytest"], "full_prompt": "run tests"},
        chat={"turns": [
            {"role": "user", "content": "implement feature", "ts": None},
            {"role": "assistant", "content": "done", "ts": None},
        ]},
        api_errors=[],
        stdout=stdout,
    )


class TestBuildLiveSessionTxt:
    def test_not_valid_json(self):
        data = _make_session_data()
        txt = panel._build_live_session_txt(data, redactor=None)
        try:
            json.loads(txt)
            assert False, "should not be valid JSON"
        except (json.JSONDecodeError, ValueError):
            pass

    def test_has_schema_header(self):
        data = _make_session_data()
        txt = panel._build_live_session_txt(data, redactor=None)
        assert "# deile export: live_session" in txt
        assert "# schema: deile.export.v2" in txt

    def test_has_session_section(self):
        data = _make_session_data()
        txt = panel._build_live_session_txt(data, redactor=None)
        assert "[SESSION]" in txt
        assert "task_id" in txt

    def test_has_stdout_section(self):
        data = _make_session_data(stdout="hello stdout\nline2")
        txt = panel._build_live_session_txt(data, redactor=None)
        assert "[STDOUT]" in txt
        assert "hello stdout" in txt

    def test_stdout_none_section(self):
        data = _make_session_data(stdout=None)
        txt = panel._build_live_session_txt(data, redactor=None)
        assert "[STDOUT]" in txt
        assert "(none)" in txt

    def test_redaction_removes_secret(self):
        from deile.security.secrets_scanner import SecretsScanner
        redactor = SecretsScanner()
        secret_value = "ghp_" + "A" * 36
        data = _make_session_data(stdout=f"token={secret_value}")
        txt = panel._build_live_session_txt(data, redactor=redactor)
        assert secret_value not in txt


class TestBuildPodWatchTxt:
    def test_not_valid_json(self):
        txt = panel._build_pod_watch_txt("my-pod", "worker", ["log line 1"], redactor=None)
        try:
            json.loads(txt)
            assert False, "should not be valid JSON"
        except (json.JSONDecodeError, ValueError):
            pass

    def test_has_header(self):
        txt = panel._build_pod_watch_txt("my-pod", "worker", ["line1", "line2"], redactor=None)
        assert "# deile export: pod_watch" in txt
        assert "# pod: my-pod" in txt
        assert "# role: worker" in txt
        assert "line1" in txt
        assert "line2" in txt


class TestTxtExtensionDetection:
    def test_txt_extension_sets_export_txt(self, tmp_path):
        v = panel.LiveSessionView()
        v._last_render = MagicMock(
            session={"task_id": "t1"}, command=None, chat=None,
            api_errors=[], stdout=None,
        )
        v.handle_key("E", MagicMock())
        target = tmp_path / "out.txt"
        v._export_path_buf = str(target)
        v.handle_key("\r", MagicMock())
        assert v._export_txt is True

    def test_json_extension_no_txt(self, tmp_path):
        v = panel.LiveSessionView()
        v._last_render = MagicMock(
            session={"task_id": "t1"}, command=None, chat=None,
            api_errors=[], stdout=None,
        )
        v.handle_key("E", MagicMock())
        target = tmp_path / "out.json"
        v._export_path_buf = str(target)
        v.handle_key("\r", MagicMock())
        assert v._export_txt is False
