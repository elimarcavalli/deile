"""Tests for issue #446: ACTIVITY widget drill-down.

Coverage:
  1.  ActivityEvent.source_pod defaults to "" (retrocompatible).
  2.  ActivityEvent.task_id defaults to None (retrocompatible).
  3.  ActivityEvent source_pod and task_id can be set at construction time.
  4.  DashboardView initial state: _activity_focused=False, _activity_cursor=0.
  5.  Key [a] enters activity cursor mode, cursor resets to 0.
  6.  Key [esc] exits cursor mode.
  7.  Up arrow wraps cursor from 0 to last row.
  8.  Down arrow advances cursor.
  9.  [k] moves up, [j] moves down.
  10. [enter] on claude-worker row with task_id -> nav("live-session").
  11. [enter] on non-claude-worker row with source_pod -> nav("pod-watch").
  12. [enter] on row without source_pod -> refresh (no nav).
  13. [enter] with empty event list -> refresh.
  14. intercepts_key returns True for ESC/arrows/jk/enter when focused.
  15. intercepts_key returns False for navigation keys when focused.
  16. intercepts_key returns False for ESC when not focused.
  17. LiveSessionView is registered under "live-session" in _build_views().
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

_UTC = timezone.utc


def _ts() -> datetime:
    return datetime(2026, 1, 1, 16, 0, 0, tzinfo=_UTC)


def _event(actor: str = "pipeline", source_pod: str = "",
           task_id=None) -> pd.ActivityEvent:
    return pd.ActivityEvent(
        ts=_ts(), actor=actor, action="dispatch",
        target="#1", detail="", source_pod=source_pod, task_id=task_id,
    )


def _mock_app() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# 1-3: ActivityEvent new fields
# ---------------------------------------------------------------------------

class TestActivityEventNewFields:
    def test_source_pod_default(self):
        ev = pd.ActivityEvent(ts=_ts(), actor="p", action="a", target="t", detail="d")
        assert ev.source_pod == ""

    def test_task_id_default(self):
        ev = pd.ActivityEvent(ts=_ts(), actor="p", action="a", target="t", detail="d")
        assert ev.task_id is None

    def test_fields_settable(self):
        ev = pd.ActivityEvent(
            ts=_ts(), actor="claude-worker", action="dispatch",
            target="#5", detail="x", source_pod="claude-worker-abc-xyz",
            task_id="task-123",
        )
        assert ev.source_pod == "claude-worker-abc-xyz"
        assert ev.task_id == "task-123"


# ---------------------------------------------------------------------------
# 4: Initial state
# ---------------------------------------------------------------------------

class TestDashboardViewInitialState:
    def test_activity_focused_false(self):
        v = panel.DashboardView()
        assert v._activity_focused is False

    def test_activity_cursor_zero(self):
        v = panel.DashboardView()
        assert v._activity_cursor == 0


# ---------------------------------------------------------------------------
# 5-6: Entering / exiting focus mode
# ---------------------------------------------------------------------------

class TestDashboardViewFocusToggle:
    def test_a_key_enters_focus(self):
        v = panel.DashboardView()
        result = v.handle_key("a", _mock_app())
        assert v._activity_focused is True
        assert v._activity_cursor == 0
        assert result.kind == panel.Action.REFRESH

    def test_escape_exits_focus(self):
        v = panel.DashboardView()
        v._activity_focused = True
        v._last_activity_events = [_event()]
        result = v.handle_key("\x1b", _mock_app())
        assert v._activity_focused is False
        assert result.kind == panel.Action.REFRESH


# ---------------------------------------------------------------------------
# 7-9: Cursor navigation
# ---------------------------------------------------------------------------

class TestDashboardViewCursorNavigation:
    def _view_with_events(self, n: int = 3) -> panel.DashboardView:
        v = panel.DashboardView()
        v._activity_focused = True
        v._activity_cursor = 0
        v._last_activity_events = [_event() for _ in range(n)]
        return v

    def test_up_arrow_wraps(self):
        v = self._view_with_events(3)
        v.handle_key("\x1b[A", _mock_app())
        assert v._activity_cursor == 2  # 0 - 1 mod 3

    def test_down_arrow_advances(self):
        v = self._view_with_events(3)
        v.handle_key("\x1b[B", _mock_app())
        assert v._activity_cursor == 1

    def test_k_moves_up(self):
        v = self._view_with_events(3)
        v._activity_cursor = 2
        v.handle_key("k", _mock_app())
        assert v._activity_cursor == 1

    def test_j_moves_down(self):
        v = self._view_with_events(3)
        v._activity_cursor = 1
        v.handle_key("j", _mock_app())
        assert v._activity_cursor == 2


# ---------------------------------------------------------------------------
# 10-13: Drill-down dispatch on [enter]
# ---------------------------------------------------------------------------

class TestDashboardViewDrillDown:
    def _view_with(self, ev: pd.ActivityEvent) -> panel.DashboardView:
        v = panel.DashboardView()
        v._activity_focused = True
        v._activity_cursor = 0
        v._last_activity_events = [ev]
        return v

    def test_enter_claude_worker_with_task_id_navs_live_session(self):
        ev = _event(actor="claude-worker", source_pod="claude-worker-xyz", task_id="task-abc")
        v = self._view_with(ev)
        result = v.handle_key("\r", _mock_app())
        assert result.kind == panel.Action.NAV
        assert result.target == "live-session"
        assert result.payload["task_id"] == "task-abc"
        assert result.payload["pod_name"] == "claude-worker-xyz"

    def test_enter_non_claude_worker_with_pod_navs_pod_watch(self):
        ev = _event(actor="deile-worker", source_pod="deile-worker-abc-1")
        v = self._view_with(ev)
        result = v.handle_key("\r", _mock_app())
        assert result.kind == panel.Action.NAV
        assert result.target == "pod-watch"
        assert result.payload["pod_name"] == "deile-worker-abc-1"
        assert result.payload["pod_role"] == "deile-worker"

    def test_enter_claude_worker_without_task_id_falls_back_to_pod_watch(self):
        ev = _event(actor="claude-worker", source_pod="claude-worker-xyz", task_id=None)
        v = self._view_with(ev)
        result = v.handle_key("\r", _mock_app())
        # No task_id -> fallback to pod-watch since source_pod is set
        assert result.kind == panel.Action.NAV
        assert result.target == "pod-watch"

    def test_enter_no_pod_refreshes(self):
        ev = _event(actor="pipeline", source_pod="")
        v = self._view_with(ev)
        result = v.handle_key("\r", _mock_app())
        assert result.kind == panel.Action.REFRESH

    def test_enter_empty_events_refreshes(self):
        v = panel.DashboardView()
        v._activity_focused = True
        v._activity_cursor = 0
        v._last_activity_events = []
        result = v.handle_key("\r", _mock_app())
        assert result.kind == panel.Action.REFRESH

    def test_enter_newline_also_works(self):
        ev = _event(actor="claude-worker", source_pod="pod-x", task_id="t-1")
        v = self._view_with(ev)
        result = v.handle_key("\n", _mock_app())
        assert result.kind == panel.Action.NAV
        assert result.target == "live-session"


# ---------------------------------------------------------------------------
# 14-16: intercepts_key
# ---------------------------------------------------------------------------

class TestDashboardViewInterceptsKey:
    def test_intercepts_esc_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("\x1b") is True

    def test_intercepts_up_arrow_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("\x1b[A") is True

    def test_intercepts_down_arrow_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("\x1b[B") is True

    def test_intercepts_enter_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("\r") is True

    def test_intercepts_j_k_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("j") is True
        assert v.intercepts_key("k") is True

    def test_does_not_intercept_r_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("r") is False

    def test_does_not_intercept_esc_when_not_focused(self):
        v = panel.DashboardView()
        v._activity_focused = False
        assert v.intercepts_key("\x1b") is False


# ---------------------------------------------------------------------------
# 17: LiveSessionView registered
# ---------------------------------------------------------------------------

class TestLiveSessionViewRegistered:
    def test_live_session_in_build_views(self):
        views = panel._build_views()
        assert "live-session" in views
        assert isinstance(views["live-session"], panel.LiveSessionView)
