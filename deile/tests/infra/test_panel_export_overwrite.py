"""Tests: atomic write and overwrite confirmation for panel export (#547)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel


def _make_live_view_with_data():
    v = panel.LiveSessionView()
    v._last_render = MagicMock(
        session={"task_id": "t1"},
        command={"cmd": ["cc"]},
        chat={"turns": []},
        api_errors=[],
        stdout=None,
    )
    v.task_id = "t1"
    return v


class TestWriteAtomic:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "out.json"
        panel._write_atomic(b'{"test": 1}', target)
        assert target.read_bytes() == b'{"test": 1}'

    def test_uses_os_replace(self, tmp_path):
        target = tmp_path / "out.json"
        replaced = []
        orig_replace = os.replace
        def mock_replace(src, dst):
            replaced.append((src, dst))
            orig_replace(src, dst)
        with patch.object(os, "replace", side_effect=mock_replace):
            panel._write_atomic(b"hello", target)
        assert len(replaced) == 1
        assert Path(replaced[0][1]) == target

    def test_tmp_in_same_dir(self, tmp_path):
        target = tmp_path / "out.json"
        tmp_paths = []
        orig_mkstemp = __import__("tempfile").mkstemp
        def mock_mkstemp(dir=None, **kw):
            fd, path = orig_mkstemp(dir=dir, **kw)
            tmp_paths.append(Path(path))
            return fd, path
        with patch("tempfile.mkstemp", side_effect=mock_mkstemp):
            panel._write_atomic(b"data", target)
        assert len(tmp_paths) == 1
        assert tmp_paths[0].parent == target.parent

    def test_new_file_no_confirmation(self, tmp_path):
        v = _make_live_view_with_data()
        target = tmp_path / "new.json"
        v.handle_key("E", MagicMock())
        v._export_path_buf = str(target)
        v.handle_key("\r", MagicMock())
        assert target.exists()
        assert v._export_mode is None

    def test_existing_file_triggers_overwrite_prompt(self, tmp_path):
        v = _make_live_view_with_data()
        target = tmp_path / "exists.json"
        target.write_text("old content")
        v.handle_key("E", MagicMock())
        v._export_path_buf = str(target)
        v.handle_key("\r", MagicMock())
        assert v._export_mode == "overwrite"
        assert v._export_target == target

    def test_y_confirms_overwrite(self, tmp_path):
        v = _make_live_view_with_data()
        target = tmp_path / "exists.json"
        target.write_text("old content")
        v.handle_key("E", MagicMock())
        v._export_path_buf = str(target)
        v.handle_key("\r", MagicMock())
        v.handle_key("y", MagicMock())
        assert v._export_mode is None
        content = target.read_bytes()
        assert b"deile.export.v2" in content

    def test_n_cancels_overwrite(self, tmp_path):
        v = _make_live_view_with_data()
        target = tmp_path / "exists.json"
        target.write_text("old content")
        v.handle_key("E", MagicMock())
        v._export_path_buf = str(target)
        v.handle_key("\r", MagicMock())
        v.handle_key("n", MagicMock())
        assert v._export_mode is None
        assert target.read_text() == "old content"
