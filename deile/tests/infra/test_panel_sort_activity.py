"""Tests for issue #393: sort by recent activity + last-activity footer indicator.

Coverage:
  1. ``_pod_rows`` sort_mode="recent" — rows ordered by age_s asc; "—" last.
  2. ``_pod_rows`` sort_mode="number" — rows ordered by pod name asc.
  3. ``_pod_rows`` sort_mode="status" — Running first.
  4. ``IssuesPRsView._rows`` sort_mode="recent" — most-recent updated_at first; None last.
  5. ``IssuesPRsView._rows`` sort_mode="number" — ascending by issue number.
  6. ``IssuesPRsView._rows`` sort_mode="status" — ordered by workflow priority.
  7. Hotkey ``[s]`` on DashboardView cycles recent → number → status → recent.
  8. Hotkey ``[s]`` on IssuesPRsView cycles independently; resets cursor.
  9. ``DashboardView.HOTKEYS`` reflects current sort_mode.
  10. ``IssuesPRsView.HOTKEYS`` reflects current sort_mode.
  11. ``_last_activity_caption`` returns None when data is None.
  12. ``_last_activity_caption`` returns None when no events.
  13. ``_last_activity_caption`` returns caption for most-recent event.
  14. ``_footer_panel`` without last_activity renders one line.
  15. ``_footer_panel`` with last_activity renders two lines (hotkeys + indicator).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

from rich.console import Console  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_activity_event(age_s: float, target: str = "#1",
                          action: str = "dispatch",
                          detail: str = "") -> "pd.ActivityEvent":
    ts = _utc_now() - timedelta(seconds=age_s)
    return pd.ActivityEvent(ts=ts, actor="pipeline", action=action,
                             target=target, detail=detail)


def _make_pipeline_state(events: list) -> "pd.PipelineState":
    ps = pd.PipelineState()
    ps.events = events
    return ps


def _fake_data(pipeline_events=None, local_events=None):
    """Build a minimal PanelData-like mock."""
    data = MagicMock()
    ps = _make_pipeline_state(pipeline_events or [])
    data.pipeline.get.return_value = ps
    if local_events is not None:
        ls = pd.LocalLogsState()
        ls.events = local_events
        data.local_logs.get.return_value = ls
    else:
        data.local_logs = None
    return data


def _make_pod_row(name: str, status: str = "Running",
                  last_activity: str = "1s ago",
                  age_s_for_sort: float = 1.0) -> "panel.PodRow":
    return panel.PodRow(icon="●", name=name, role="other", status=status,
                        age="1s", restarts="0",
                        last_activity=last_activity, doing_now="—", busy=False)


def _make_issue(number: int, updated_at=None, workflow: str = "nova",
                is_pr: bool = False) -> "pd.GitHubIssue":
    return pd.GitHubIssue(
        number=number,
        title=f"issue {number}",
        is_pr=is_pr,
        state="open",
        labels=[f"~workflow:{workflow}"] if workflow else [],
        assignees=[],
        updated_at=updated_at,
        url=f"https://github.com/x/y/issues/{number}",
        workflow=workflow,
        review="",
        blocked=False,
    )


class _FakeGitHubProvider:
    def __init__(self, issues, prs):
        self._snap = pd.GitHubSnapshot(issues=issues, prs=prs)

    def get(self, force=False):
        return self._snap


class _FakePanelData:
    def __init__(self, issues, prs, pipeline_events=None, local_events=None):
        self.github = _FakeGitHubProvider(issues, prs)
        ps = _make_pipeline_state(pipeline_events or [])
        self.pipeline = MagicMock()
        self.pipeline.get.return_value = ps
        if local_events is not None:
            ls = pd.LocalLogsState()
            ls.events = local_events
            self.local_logs = MagicMock()
            self.local_logs.get.return_value = ls
        else:
            self.local_logs = None


# ---------------------------------------------------------------------------
# _pod_rows sorting
# ---------------------------------------------------------------------------

class TestPodRowsSort:
    def _make_data_with_pods(self, pod_specs):
        """pod_specs: list of (name, role, last_activity_s)
        last_activity_s: seconds since last activity (None → no activity).
        """
        data = MagicMock()
        pods = []
        workers = {}
        now = _utc_now()
        for name, role, age_s in pod_specs:
            p = MagicMock()
            p.name = name
            p.role = role
            p.status = "Running"
            p.ready = True
            p.restarts = 0
            p.age_s = 100.0
            pods.append(p)
            if role == "worker":
                ws = pd.WorkerState(pod_name=name)
                # last_activity_s is computed from last_substantive_ts.
                if age_s is not None:
                    ws.last_substantive_ts = now - timedelta(seconds=age_s)
                ws.busy = False
                ws.last_substantive_body = ""
                workers[name] = ws
        data.pods.get.return_value = pods
        data.workers.get.return_value = workers
        # Use MagicMock for PipelineState since last_action_age_s is a computed property.
        ps = MagicMock()
        ps.last_action_age_s = None
        ps.last_action_summary = ""
        ps.events = []
        data.pipeline.get.return_value = ps
        return data

    def test_sort_recent_orders_by_age_asc(self):
        # worker-b was active 2s ago, worker-a was active 10s ago → b first
        data = self._make_data_with_pods([
            ("worker-a", "worker", 10.0),
            ("worker-b", "worker", 2.0),
        ])
        rows = panel._pod_rows(data, sort_mode="recent")
        names = [r.name for r in rows]
        assert names.index("worker-b") < names.index("worker-a")

    def test_sort_recent_puts_dash_last(self):
        # "other" role has no activity → goes to end
        data = self._make_data_with_pods([
            ("bot", "other", None),
            ("worker-a", "worker", 5.0),
        ])
        rows = panel._pod_rows(data, sort_mode="recent")
        assert rows[-1].name == "bot"

    def test_sort_number_orders_by_name_alpha(self):
        data = self._make_data_with_pods([
            ("worker-z", "worker", 1.0),
            ("worker-a", "worker", 100.0),
        ])
        rows = panel._pod_rows(data, sort_mode="number")
        assert rows[0].name == "worker-a"
        assert rows[1].name == "worker-z"

    def test_sort_status_running_first(self):
        data = MagicMock()
        pods = []
        p_nr = MagicMock()
        p_nr.name = "not-ready-pod"
        p_nr.role = "other"
        p_nr.status = "Running"
        p_nr.ready = False  # → NotReady
        p_nr.restarts = 0
        p_nr.age_s = 10.0
        p_r = MagicMock()
        p_r.name = "running-pod"
        p_r.role = "other"
        p_r.status = "Running"
        p_r.ready = True
        p_r.restarts = 0
        p_r.age_s = 10.0
        pods = [p_nr, p_r]
        data.pods.get.return_value = pods
        data.workers.get.return_value = {}
        data.pipeline.get.return_value = pd.PipelineState()
        rows = panel._pod_rows(data, sort_mode="status")
        assert rows[0].name == "running-pod"

    def test_default_sort_mode_is_recent(self):
        # Calling without sort_mode should default to "recent"
        data = self._make_data_with_pods([
            ("worker-a", "worker", 10.0),
            ("worker-b", "worker", 2.0),
        ])
        rows_default = panel._pod_rows(data)
        rows_recent = panel._pod_rows(data, sort_mode="recent")
        assert [r.name for r in rows_default] == [r.name for r in rows_recent]


# ---------------------------------------------------------------------------
# IssuesPRsView._rows sorting
# ---------------------------------------------------------------------------

class TestIssuesPRsViewSort:
    def test_sort_recent_most_recent_first(self):
        now = _utc_now()
        old = _make_issue(1, updated_at=now - timedelta(hours=2))
        new = _make_issue(2, updated_at=now - timedelta(seconds=30))
        view = panel.IssuesPRsView(data=_FakePanelData([old, new], []))
        view.sort_mode = "recent"
        issues, _ = view._rows()
        assert issues[0].number == 2
        assert issues[1].number == 1

    def test_sort_recent_none_updated_at_goes_last(self):
        now = _utc_now()
        has_date = _make_issue(1, updated_at=now - timedelta(hours=1))
        no_date = _make_issue(2, updated_at=None)
        view = panel.IssuesPRsView(data=_FakePanelData([has_date, no_date], []))
        view.sort_mode = "recent"
        issues, _ = view._rows()
        assert issues[-1].number == 2

    def test_sort_number_ascending(self):
        now = _utc_now()
        i5 = _make_issue(5, updated_at=now)
        i2 = _make_issue(2, updated_at=now)
        i9 = _make_issue(9, updated_at=now)
        view = panel.IssuesPRsView(data=_FakePanelData([i5, i2, i9], []))
        view.sort_mode = "number"
        issues, _ = view._rows()
        assert [it.number for it in issues] == [2, 5, 9]

    def test_sort_status_by_workflow_priority(self):
        # em_implementacao (0) should come before nova (4)
        i_nova = _make_issue(1, workflow="nova")
        i_impl = _make_issue(2, workflow="em_implementacao")
        view = panel.IssuesPRsView(data=_FakePanelData([i_nova, i_impl], []))
        view.sort_mode = "status"
        issues, _ = view._rows()
        assert issues[0].number == 2  # em_implementacao first

    def test_sort_is_independent_per_view_instance(self):
        # Two views with different sort_modes must not interfere.
        now = _utc_now()
        issues = [_make_issue(3, updated_at=now - timedelta(hours=1)),
                  _make_issue(1, updated_at=now - timedelta(seconds=10))]
        data = _FakePanelData(issues, [])
        view_recent = panel.IssuesPRsView(data=data)
        view_recent.sort_mode = "recent"
        view_number = panel.IssuesPRsView(data=data)
        view_number.sort_mode = "number"
        recent_issues, _ = view_recent._rows()
        number_issues, _ = view_number._rows()
        assert recent_issues[0].number == 1   # most recent first
        assert number_issues[0].number == 1   # lowest number first (also 1 here)
        # Make it unambiguous: recent sort returns #1 first, number sort returns #1 first too.
        # Redo with clearer numbers.
        issues2 = [_make_issue(10, updated_at=now - timedelta(hours=1)),
                   _make_issue(2, updated_at=now - timedelta(seconds=10))]
        data2 = _FakePanelData(issues2, [])
        vr = panel.IssuesPRsView(data=data2)
        vr.sort_mode = "recent"
        vn = panel.IssuesPRsView(data=data2)
        vn.sort_mode = "number"
        ri, _ = vr._rows()
        ni, _ = vn._rows()
        assert ri[0].number == 2   # most recent (10 here = 2s old)
        assert ni[0].number == 2   # lowest number

    def test_prs_sorted_independently_from_issues(self):
        # PRs should also be sorted by sort_mode, not just issues.
        now = _utc_now()
        pr_old = _make_issue(100, updated_at=now - timedelta(hours=5), is_pr=True)
        pr_new = _make_issue(200, updated_at=now - timedelta(seconds=5), is_pr=True)
        view = panel.IssuesPRsView(data=_FakePanelData([], [pr_old, pr_new]))
        view.sort_mode = "recent"
        _, prs = view._rows()
        assert prs[0].number == 200


# ---------------------------------------------------------------------------
# Hotkey [s] cycling
# ---------------------------------------------------------------------------

class TestSortHotkey:
    def test_dashboard_s_cycles_recent_number_status(self):
        view = panel.DashboardView(data=None)
        assert view.sort_mode == "recent"
        view.handle_key("s", MagicMock())
        assert view.sort_mode == "number"
        view.handle_key("s", MagicMock())
        assert view.sort_mode == "status"
        view.handle_key("s", MagicMock())
        assert view.sort_mode == "recent"

    def test_issues_prs_s_cycles_independently(self):
        view = panel.IssuesPRsView(data=None)
        assert view.sort_mode == "recent"
        view.handle_key("s", MagicMock())
        assert view.sort_mode == "number"
        view.handle_key("s", MagicMock())
        assert view.sort_mode == "status"
        view.handle_key("s", MagicMock())
        assert view.sort_mode == "recent"

    def test_issues_prs_s_resets_cursor(self):
        now = _utc_now()
        issues = [_make_issue(i, updated_at=now) for i in range(5)]
        view = panel.IssuesPRsView(data=_FakePanelData(issues, []))
        view.cursor = 3
        view.handle_key("s", MagicMock())
        assert view.cursor == 0

    def test_dashboard_s_returns_refresh(self):
        view = panel.DashboardView(data=None)
        result = view.handle_key("s", MagicMock())
        assert result.kind == panel.Action.REFRESH

    def test_issues_prs_s_returns_refresh(self):
        view = panel.IssuesPRsView(data=None)
        result = view.handle_key("s", MagicMock())
        assert result.kind == panel.Action.REFRESH

    def test_two_views_cycle_independently(self):
        d = panel.DashboardView(data=None)
        i = panel.IssuesPRsView(data=None)
        d.handle_key("s", MagicMock())  # d → number
        # i should still be at recent
        assert i.sort_mode == "recent"
        assert d.sort_mode == "number"


# ---------------------------------------------------------------------------
# HOTKEYS property
# ---------------------------------------------------------------------------

class TestHotkeysProperty:
    def test_dashboard_hotkeys_reflects_sort_mode_recent(self):
        view = panel.DashboardView(data=None)
        assert "[s]ort:recent" in view.HOTKEYS

    def test_dashboard_hotkeys_reflects_sort_mode_number(self):
        view = panel.DashboardView(data=None)
        view.sort_mode = "number"
        assert "[s]ort:number" in view.HOTKEYS

    def test_dashboard_hotkeys_reflects_sort_mode_status(self):
        view = panel.DashboardView(data=None)
        view.sort_mode = "status"
        assert "[s]ort:status" in view.HOTKEYS

    def test_issues_prs_hotkeys_reflects_sort_mode(self):
        view = panel.IssuesPRsView(data=None)
        assert "[s]ort:recent" in view.HOTKEYS
        view.sort_mode = "number"
        assert "[s]ort:number" in view.HOTKEYS


# ---------------------------------------------------------------------------
# _last_activity_caption
# ---------------------------------------------------------------------------

class TestLastActivityCaption:
    def test_returns_none_when_data_is_none(self):
        assert panel._last_activity_caption(None) is None

    def test_returns_none_when_no_events(self):
        data = _fake_data(pipeline_events=[])
        assert panel._last_activity_caption(data) is None

    def test_returns_caption_for_most_recent_event(self):
        ev_old = _make_activity_event(age_s=100, target="#1")
        ev_new = _make_activity_event(age_s=5, target="#360", detail="em_pr")
        data = _fake_data(pipeline_events=[ev_old, ev_new])
        caption = panel._last_activity_caption(data)
        assert caption is not None
        assert "#360" in caption
        assert "em_pr" in caption

    def test_caption_contains_age(self):
        ev = _make_activity_event(age_s=23, target="#100")
        data = _fake_data(pipeline_events=[ev])
        caption = panel._last_activity_caption(data)
        assert caption is not None
        assert "ago" in caption

    def test_combines_pipeline_and_local_events(self):
        ev_pipeline = _make_activity_event(age_s=60, target="#1")
        ev_local = _make_activity_event(age_s=5, target="#latest")
        data = _fake_data(pipeline_events=[ev_pipeline],
                          local_events=[ev_local])
        caption = panel._last_activity_caption(data)
        assert caption is not None
        assert "#latest" in caption

    def test_uses_action_when_detail_empty(self):
        ev = pd.ActivityEvent(
            ts=_utc_now() - timedelta(seconds=10),
            actor="pipeline", action="dispatch",
            target="#99", detail="",
        )
        data = _fake_data(pipeline_events=[ev])
        caption = panel._last_activity_caption(data)
        assert caption is not None
        assert "dispatch" in caption

    def test_falls_back_to_actor_when_target_empty(self):
        ev = pd.ActivityEvent(
            ts=_utc_now() - timedelta(seconds=10),
            actor="local", action="startup",
            target="", detail="",
        )
        data = _fake_data(pipeline_events=[ev])
        caption = panel._last_activity_caption(data)
        assert caption is not None
        assert "local" in caption


# ---------------------------------------------------------------------------
# _footer_panel with last_activity
# ---------------------------------------------------------------------------

class TestFooterPanel:
    def _render_footer(self, hotkeys: str, last_activity=None) -> str:
        footer = panel._footer_panel(hotkeys, last_activity)
        console = Console(record=True, width=200, force_terminal=False,
                          color_system=None)
        console.print(footer)
        return console.export_text()

    def test_footer_without_last_activity_shows_hotkeys(self):
        out = self._render_footer("[q]uit")
        assert "[q]uit" in out

    def test_footer_without_last_activity_no_last_activity_line(self):
        out = self._render_footer("[q]uit")
        assert "Last activity:" not in out

    def test_footer_with_last_activity_shows_both_lines(self):
        out = self._render_footer("[q]uit", "23s ago — #360 → em_pr")
        assert "[q]uit" in out
        assert "Last activity:" in out
        assert "#360" in out

    def test_footer_with_none_last_activity_same_as_without(self):
        out_none = self._render_footer("[q]uit", None)
        out_empty = self._render_footer("[q]uit")
        assert out_none == out_empty
