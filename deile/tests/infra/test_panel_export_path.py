"""Tests: path editing modal and path sanitization for panel export (#461/#547)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel


def _make_view_with_data():
    v = panel.LiveSessionView()
    v._last_render = MagicMock()
    v.task_id = "test-task-123"
    return v


class TestSafeIdSegment:
    def test_normal_slug(self):
        assert panel._safe_id_segment("abc-123") == "abc-123"

    def test_slash_replaced(self):
        seg = panel._safe_id_segment("claude-worker/7d:8c")
        assert "/" not in seg
        assert ":" not in seg
        assert seg == "claude-worker_7d_8c"

    def test_truncated_at_64(self):
        long = "a" * 100
        assert len(panel._safe_id_segment(long)) == 64

    def test_empty_string(self):
        seg = panel._safe_id_segment("")
        assert seg == "unknown"


class TestExportPathModal:
    def test_E_opens_modal_when_data_ready(self):
        v = _make_view_with_data()
        result = v.handle_key("E", MagicMock())
        assert v._export_mode == "path"
        assert v._export_path_buf.endswith(".json")
        assert "live_session" in v._export_path_buf

    def test_E_no_data_shows_status(self):
        v = panel.LiveSessionView()
        v._last_render = None
        result = v.handle_key("E", MagicMock())
        assert v._export_mode is None
        assert v._status_msg is not None

    def test_printable_chars_append_to_buf(self):
        v = _make_view_with_data()
        v.handle_key("E", MagicMock())
        original = v._export_path_buf
        v.handle_key("x", MagicMock())
        assert v._export_path_buf == original + "x"

    def test_backspace_removes_last_char(self):
        v = _make_view_with_data()
        v.handle_key("E", MagicMock())
        v._export_path_buf = "/tmp/test.json"
        v.handle_key("\x7f", MagicMock())
        assert v._export_path_buf == "/tmp/test.jso"

    def test_esc_cancels_without_writing(self, tmp_path):
        v = _make_view_with_data()
        v.handle_key("E", MagicMock())
        target = tmp_path / "should_not_exist.json"
        v._export_path_buf = str(target)
        v.handle_key("\x1b", MagicMock())
        assert v._export_mode is None
        assert not target.exists()

    def test_enter_writes_file(self, tmp_path):
        v = _make_view_with_data()
        v._last_render = MagicMock(
            session={"task_id": "t1"},
            command={"cmd": ["python3"]},
            chat={"turns": []},
            api_errors=[],
            stdout=None,
        )
        v._history.append({"polled_at": "2026-01-01T00:00:00Z",
                           "session": None, "command": None, "chat": None})
        v.handle_key("E", MagicMock())
        target = tmp_path / "out.json"
        v._export_path_buf = str(target)
        v.handle_key("\r", MagicMock())
        assert v._export_mode is None
        assert target.exists()

    def test_default_path_pre_filled(self):
        v = _make_view_with_data()
        v.handle_key("E", MagicMock())
        buf = v._export_path_buf
        assert "live_session" in buf
        assert buf.endswith(".json")
