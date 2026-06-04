"""Tests: history ring buffer and dedup for LiveSessionView export (#547)."""
from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel
from deile.ui.panel.observability.screens import LiveSessionData


class TestHistoryRingBuffer:
    def test_maxlen_200(self):
        v = panel.LiveSessionView()
        assert v._history.maxlen == 200

    def test_consecutive_dedup(self):
        v = panel.LiveSessionView()
        snap = {"polled_at": "2026-01-01T00:00:00Z",
                "session": {"task_id": "t1"},
                "command": None, "chat": None}
        snap_key = json.dumps(snap, sort_keys=True, default=str)
        # First add
        if snap_key != v._history_last_key:
            v._history.append(snap)
            v._history_last_key = snap_key
        # Second add same snap — should be deduped
        if snap_key != v._history_last_key:
            v._history.append(snap)
            v._history_last_key = snap_key
        assert len(v._history) == 1

    def test_different_snaps_both_kept(self):
        v = panel.LiveSessionView()
        snaps = [
            {"polled_at": "2026-01-01T00:00:00Z", "session": {"task_id": "t1"},
             "command": None, "chat": None},
            {"polled_at": "2026-01-01T00:00:01Z", "session": {"task_id": "t2"},
             "command": None, "chat": None},
        ]
        for snap in snaps:
            key = json.dumps(snap, sort_keys=True, default=str)
            if key != v._history_last_key:
                v._history.append(snap)
                v._history_last_key = key
        assert len(v._history) == 2

    def test_history_in_export_v2(self):
        history = [
            {"polled_at": "2026-01-01T00:00:00Z", "session": None, "command": None, "chat": None}
        ]
        data = LiveSessionData(
            session=None, command=None, chat=None, api_errors=[], stdout=None
        )
        obj = panel._build_live_session_json(data, history, redactor=None)
        assert obj["schema_version"] == "deile.export.v2"
        assert len(obj["history"]) == 1
        assert obj["history"][0]["polled_at"] == "2026-01-01T00:00:00Z"

    def test_empty_history_still_v2(self):
        data = LiveSessionData(
            session=None, command=None, chat=None, api_errors=[], stdout=None
        )
        obj = panel._build_live_session_json(data, [], redactor=None)
        assert obj["schema_version"] == "deile.export.v2"
        assert obj["history"] == []
