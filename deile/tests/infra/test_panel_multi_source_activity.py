"""Tests for issue #436: MultiSourceActivityProvider.

Coverage:
  1.  _classify_canonical returns None for non-canonical lines.
  2.  _classify_canonical parses dispatch.started correctly.
  3.  _classify_canonical parses dispatch.completed with outcome.
  4.  _classify_canonical parses inbound.mention with target extraction.
  5.  _classify_canonical parses monitor.tick/action.
  6.  _classify_canonical parses git.commit with branch target.
  7.  _classify_source_line: canonical path is taken when canonical matches.
  8.  _classify_source_line: falls back to legacy _classify_pipeline_line.
  9.  _classify_source_line: overrides actor from legacy with supplied role.
  10. _classify_source_line: returns None when neither parser matches.
  11. MultiSourceActivityState.top() returns events sorted desc, capped at N.
  12. MultiSourceActivityState.top() with empty events returns [].
  13. MultiSourceActivityProvider._fetch_source: returns [] on empty text.
  14. MultiSourceActivityProvider._fetch builds rolling buffer capped at 200.
  15. MultiSourceActivityProvider.get() returns MultiSourceActivityState.
  16. _activity_from_data uses activity provider when present.
  17. _activity_from_data falls back to pipeline+local when activity is None.
  18. _activity_panel renders table without literal width columns.
  19. _last_activity_caption uses activity provider when present.
  20. _last_activity_caption falls back to pipeline+local when activity is None.
  AC18. Each source can be in intermediate state — provider stays functional.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from typing import List
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

_UTC = timezone.utc


def _utc_now() -> datetime:
    return datetime.now(_UTC)


def _make_logline(body: str, age_s: float = 0.0) -> pd.LogLine:
    ts = _utc_now() - timedelta(seconds=age_s)
    return pd.LogLine(ts=ts, body=body)


def _make_event(age_s: float = 0.0, actor: str = "pipeline",
                action: str = "dispatch", target: str = "#1",
                detail: str = "") -> pd.ActivityEvent:
    ts = _utc_now() - timedelta(seconds=age_s)
    return pd.ActivityEvent(ts=ts, actor=actor, action=action,
                            target=target, detail=detail)


# ---------------------------------------------------------------------------
# 1–6: _classify_canonical
# ---------------------------------------------------------------------------

class TestClassifyCanonical:
    def test_returns_none_for_non_canonical(self):
        ll = _make_logline("worker dispatch starting some task")
        assert pd._classify_canonical(ll, "pipeline") is None

    def test_parses_dispatch_started(self):
        ll = _make_logline(
            "dispatch.started task=abc123 channel=pipeline-issue-360 stage=implement"
        )
        ev = pd._classify_canonical(ll, "deile-worker")
        assert ev is not None
        assert ev.actor == "deile-worker"
        assert ev.action == "dispatch.started"
        assert ev.target == "#360"

    def test_parses_dispatch_completed_with_outcome(self):
        ll = _make_logline("dispatch.completed task=abc outcome=DONE duration=45s")
        ev = pd._classify_canonical(ll, "deile-worker")
        assert ev is not None
        assert ev.action == "dispatch.completed"
        assert "DONE" in ev.detail

    def test_parses_inbound_mention_issue(self):
        ll = _make_logline("inbound.mention target=issue:420 triggers=[implement]")
        ev = pd._classify_canonical(ll, "bot")
        assert ev is not None
        assert ev.action == "inbound.mention"
        assert ev.target == "#420"

    def test_parses_inbound_mention_pr(self):
        ll = _make_logline("inbound.mention target=pr:99")
        ev = pd._classify_canonical(ll, "bot")
        assert ev is not None
        assert ev.target == "PR#99"

    def test_parses_monitor_tick(self):
        ll = _make_logline("monitor.tick queue=3 pending=1")
        ev = pd._classify_canonical(ll, "monitor")
        assert ev is not None
        assert ev.action == "monitor.tick"
        assert "queue=3" in ev.detail

    def test_parses_monitor_action(self):
        ll = _make_logline("monitor.action kind=restart target=deile-worker")
        ev = pd._classify_canonical(ll, "monitor")
        assert ev is not None
        assert ev.action == "monitor.action"

    def test_parses_git_commit_with_branch(self):
        ll = _make_logline("git.commit sha=abcdef12 branch=auto/issue-360 msg=fix")
        ev = pd._classify_canonical(ll, "claude-worker")
        assert ev is not None
        assert ev.action == "git.commit"
        assert ev.target == "auto/issue-360"

    def test_parses_forge_verb(self):
        ll = _make_logline("forge.pr_created number=361")
        ev = pd._classify_canonical(ll, "pipeline")
        assert ev is not None
        assert ev.action == "forge.pr_created"

    def test_parses_refinement_verb(self):
        ll = _make_logline("refinement.started issue=437")
        ev = pd._classify_canonical(ll, "pipeline")
        assert ev is not None
        assert ev.action == "refinement.started"
        assert ev.target == "#437"

    def test_parses_cron_verb(self):
        ll = _make_logline("cron.triggered job=daily-cleanup")
        ev = pd._classify_canonical(ll, "bot")
        assert ev is not None
        assert ev.action == "cron.triggered"
        assert "daily-cleanup" in ev.detail


# ---------------------------------------------------------------------------
# 7–10: _classify_source_line dual-mode
# ---------------------------------------------------------------------------

class TestClassifySourceLine:
    def test_canonical_path_taken_when_matches(self):
        ll = _make_logline("dispatch.started task=x channel=pipeline-issue-1")
        ev = pd._classify_source_line(ll, "deile-worker")
        assert ev is not None
        assert ev.actor == "deile-worker"
        assert "dispatch" in ev.action

    def test_falls_back_to_legacy(self):
        ll = _make_logline("worker dispatch starting legacy line")
        ev = pd._classify_source_line(ll, "deile-worker")
        assert ev is not None
        # The legacy parser sets actor="pipeline"; we override to role.
        assert ev.actor == "deile-worker"
        assert ev.action == "dispatch"

    def test_legacy_actor_overridden_with_role(self):
        ll = _make_logline(
            "mention group issue:360: triggers=[implement,review]"
        )
        ev = pd._classify_source_line(ll, "bot")
        assert ev is not None
        assert ev.actor == "bot"

    def test_returns_none_when_no_parser_matches(self):
        ll = _make_logline("INFO arbitrary unstructured line with no pattern")
        ev = pd._classify_source_line(ll, "monitor")
        assert ev is None


# ---------------------------------------------------------------------------
# 11–12: MultiSourceActivityState.top()
# ---------------------------------------------------------------------------

class TestMultiSourceActivityState:
    def test_top_returns_sorted_desc_and_capped(self):
        state = pd.MultiSourceActivityState()
        state.events = [
            _make_event(age_s=10, actor="pipeline"),
            _make_event(age_s=5,  actor="bot"),
            _make_event(age_s=20, actor="monitor"),
        ]
        top = state.top(2)
        assert len(top) == 2
        # Most recent first: age_s=5 is newest
        assert top[0].actor == "bot"
        assert top[1].actor == "pipeline"

    def test_top_empty_returns_empty(self):
        state = pd.MultiSourceActivityState()
        assert state.top(10) == []

    def test_top_respects_n_cap(self):
        state = pd.MultiSourceActivityState()
        state.events = [_make_event(age_s=float(i)) for i in range(20)]
        assert len(state.top(5)) == 5


# ---------------------------------------------------------------------------
# 13–15: MultiSourceActivityProvider
# ---------------------------------------------------------------------------

class TestMultiSourceActivityProvider:
    def _make_provider(self) -> pd.MultiSourceActivityProvider:
        p = pd.MultiSourceActivityProvider(ttl_s=60.0, namespace="test",
                                           enabled=False)
        return p

    def _ok(self, stdout: str = "") -> CompletedProcess:
        return CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    def _fail(self) -> CompletedProcess:
        return CompletedProcess(args=[], returncode=1, stdout="", stderr="err")

    def test_fetch_source_returns_empty_on_none_text(self):
        p = self._make_provider()
        p._kubectl = "/usr/bin/kubectl"
        with patch("subprocess.run", return_value=self._fail()):
            result = p._fetch_source("deile-pipeline", "pipeline")
        assert result == []

    def test_fetch_source_returns_empty_on_empty_string(self):
        p = self._make_provider()
        p._kubectl = "/usr/bin/kubectl"
        with patch("subprocess.run", return_value=self._ok("")):
            result = p._fetch_source("deile-pipeline", "pipeline")
        assert result == []

    def test_fetch_source_parses_canonical_line(self):
        p = self._make_provider()
        p._kubectl = "/usr/bin/kubectl"
        ts = _utc_now().strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        log_text = f"{ts} dispatch.started task=x channel=pipeline-issue-10\n"
        with patch("subprocess.run", return_value=self._ok(log_text)):
            result = p._fetch_source("deile-worker", "deile-worker")
        assert len(result) == 1
        assert result[0].actor == "deile-worker"
        assert result[0].target == "#10"

    def test_fetch_source_parses_legacy_line(self):
        p = self._make_provider()
        p._kubectl = "/usr/bin/kubectl"
        ts = _utc_now().strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        log_text = f"{ts} worker dispatch starting\n"
        with patch("subprocess.run", return_value=self._ok(log_text)):
            result = p._fetch_source("deile-pipeline", "pipeline")
        assert len(result) == 1
        assert result[0].actor == "pipeline"

    def test_fetch_source_skips_noise_lines(self):
        p = self._make_provider()
        p._kubectl = "/usr/bin/kubectl"
        ts = _utc_now().strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        log_text = f"{ts} GET /v1/health 200\n"
        with patch("subprocess.run", return_value=self._ok(log_text)):
            result = p._fetch_source("deile-pipeline", "pipeline")
        assert result == []

    def test_fetch_builds_rolling_buffer_capped_at_200(self):
        p = pd.MultiSourceActivityProvider(ttl_s=60.0, namespace="test",
                                           enabled=True)
        p._kubectl = "/usr/bin/kubectl"
        # 60 events per source; 5 sources × 60 = 300 → capped at 200.
        # Patch _BURST_THRESHOLD high so rolling-window cap is tested in isolation.
        ts = _utc_now().strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        lines = "".join(
            f"{ts} dispatch.started task=t{i} channel=pipeline-issue-{i}\n"
            for i in range(60)
        )
        with patch.object(pd, "_BURST_THRESHOLD", 10_000):
            with patch("subprocess.run",
                       return_value=CompletedProcess([], 0, lines, "")):
                state = p._fetch()
        assert len(state.events) == pd._MULTI_BUFFER_CAP

    def test_get_returns_multi_source_state(self):
        p = pd.MultiSourceActivityProvider(ttl_s=60.0, namespace="test",
                                           enabled=True)
        p._kubectl = "/usr/bin/kubectl"
        with patch("subprocess.run",
                   return_value=CompletedProcess([], 1, "", "err")):
            state = p.get(force=True)
        assert isinstance(state, pd.MultiSourceActivityState)
        assert state.events == []


# ---------------------------------------------------------------------------
# 16–17: _activity_from_data
# ---------------------------------------------------------------------------

class TestActivityFromData:
    def _make_data_with_activity(self, events: List[pd.ActivityEvent]):
        data = MagicMock()
        state = pd.MultiSourceActivityState()
        state.events = events
        data.activity = MagicMock()
        data.activity.get.return_value = state
        return data

    def _make_data_without_activity(self, pipeline_events=None,
                                    local_events=None):
        data = MagicMock()
        data.activity = None
        ps = pd.PipelineState()
        ps.events = pipeline_events or []
        data.pipeline.get.return_value = ps
        if local_events is not None:
            ls = pd.LocalLogsState()
            ls.events = local_events
            data.local_logs = MagicMock()
            data.local_logs.get.return_value = ls
        else:
            data.local_logs = None
        return data

    def test_uses_activity_provider_when_present(self):
        ev1 = _make_event(age_s=1, actor="bot")
        ev2 = _make_event(age_s=2, actor="monitor")
        data = self._make_data_with_activity([ev1, ev2])
        rows = panel._activity_from_data(data, limit=10)
        actors = [r.actor for r in rows]
        assert "bot" in actors
        assert "monitor" in actors

    def test_uses_activity_provider_respects_limit(self):
        events = [_make_event(age_s=float(i)) for i in range(20)]
        data = self._make_data_with_activity(events)
        rows = panel._activity_from_data(data, limit=5)
        assert len(rows) == 5

    def test_fallback_to_pipeline_when_activity_none(self):
        ev = _make_event(age_s=5, actor="pipeline")
        data = self._make_data_without_activity(pipeline_events=[ev])
        rows = panel._activity_from_data(data, limit=10)
        assert len(rows) == 1
        assert rows[0].actor == "pipeline"

    def test_fallback_includes_local_events(self):
        ev_p = _make_event(age_s=10, actor="pipeline")
        ev_l = _make_event(age_s=3, actor="local")
        data = self._make_data_without_activity(
            pipeline_events=[ev_p], local_events=[ev_l]
        )
        rows = panel._activity_from_data(data, limit=10)
        actors = [r.actor for r in rows]
        assert "pipeline" in actors
        assert "local" in actors

    def test_returns_demo_rows_when_data_is_none(self):
        rows = panel._activity_from_data(None, limit=3)
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# 18: _activity_panel adaptive widths
# ---------------------------------------------------------------------------

class TestActivityPanelAdaptiveWidths:
    def test_no_literal_width_in_activity_panel(self):
        """All columns in the activity table must NOT use literal width=<int>."""
        import inspect
        src = inspect.getsource(panel.DashboardView._activity_panel)
        # The pattern `width=<integer>` is forbidden per principle 15.
        import re
        matches = re.findall(r"\bwidth\s*=\s*\d+\b", src)
        assert matches == [], (
            f"Literal width=N found in _activity_panel: {matches}"
        )


# ---------------------------------------------------------------------------
# 19–20: _last_activity_caption
# ---------------------------------------------------------------------------

class TestLastActivityCaptionMultiSource:
    def _make_data_with_activity(self, events):
        data = MagicMock()
        state = pd.MultiSourceActivityState()
        state.events = events
        data.activity = MagicMock()
        data.activity.get.return_value = state
        return data

    def _make_data_without_activity(self, pipeline_events=None):
        data = MagicMock()
        data.activity = None
        ps = pd.PipelineState()
        ps.events = pipeline_events or []
        data.pipeline.get.return_value = ps
        data.local_logs = None
        return data

    def test_uses_activity_provider_for_caption(self):
        ev = _make_event(age_s=10, actor="bot", target="#420",
                         detail="inbound.mention")
        data = self._make_data_with_activity([ev])
        caption = panel._last_activity_caption(data)
        assert caption is not None
        assert "#420" in caption

    def test_returns_none_when_activity_empty(self):
        data = self._make_data_with_activity([])
        assert panel._last_activity_caption(data) is None

    def test_fallback_to_pipeline(self):
        ev = _make_event(age_s=5, actor="pipeline", target="#99",
                         detail="dispatch done")
        data = self._make_data_without_activity(pipeline_events=[ev])
        caption = panel._last_activity_caption(data)
        assert caption is not None
        assert "#99" in caption

    def test_most_recent_event_used(self):
        ev_old = _make_event(age_s=100, target="#1")
        ev_new = _make_event(age_s=3, target="#999", detail="recent")
        data = self._make_data_with_activity([ev_old, ev_new])
        caption = panel._last_activity_caption(data)
        assert caption is not None
        assert "#999" in caption


# ---------------------------------------------------------------------------
# AC18: each source can be in intermediate state — provider stays functional
# ---------------------------------------------------------------------------

class TestAC18IntermediateState:
    """Smoke-test: provider survives when individual sources fail or return no
    events.  The overall MultiSourceActivityState is returned with events from
    the working sources; no exception is raised."""

    def test_provider_functional_when_some_sources_have_no_events(self):
        p = pd.MultiSourceActivityProvider(ttl_s=60.0, namespace="test",
                                           enabled=True)
        p._kubectl = "/usr/bin/kubectl"
        ts = _utc_now().strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        # Only deile-pipeline returns events; other sources fail.
        def _fake_run(cmd, **kw):
            if "deploy/deile-pipeline" in cmd:
                return CompletedProcess(cmd, 0,
                                        f"{ts} worker dispatch starting\n", "")
            return CompletedProcess(cmd, 1, "", "err")
        with patch("subprocess.run", side_effect=_fake_run):
            state = p._fetch()
        assert isinstance(state, pd.MultiSourceActivityState)
        # At least the pipeline source contributed one event.
        assert len(state.events) >= 1
        # No crash even though 4 other sources failed.

    def test_provider_functional_when_all_sources_fail(self):
        p = pd.MultiSourceActivityProvider(ttl_s=60.0, namespace="test",
                                           enabled=True)
        p._kubectl = "/usr/bin/kubectl"
        with patch("subprocess.run",
                   return_value=CompletedProcess([], 1, "", "err")):
            state = p._fetch()
        assert isinstance(state, pd.MultiSourceActivityState)
        assert state.events == []

    def test_provider_functional_with_mixed_canonical_and_legacy(self):
        p = pd.MultiSourceActivityProvider(ttl_s=60.0, namespace="test",
                                           enabled=True)
        p._kubectl = "/usr/bin/kubectl"
        ts = _utc_now().strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        def _fake_run(cmd, **kw):
            if "deploy/deile-pipeline" in cmd:
                return CompletedProcess(cmd, 0,
                                        f"{ts} worker dispatch starting\n", "")
            if "deploy/deile-worker" in cmd:
                return CompletedProcess(
                    cmd, 0,
                    f"{ts} dispatch.started task=x channel=pipeline-issue-5\n",
                    "")
            if "deploy/deilebot" in cmd:
                return CompletedProcess(cmd, 0,
                                        f"{ts} inbound.mention target=issue:99\n",
                                        "")
            return CompletedProcess(cmd, 1, "", "err")
        with patch("subprocess.run", side_effect=_fake_run):
            state = p._fetch()
        actors = {ev.actor for ev in state.events}
        assert "pipeline" in actors      # legacy
        assert "deile-worker" in actors  # canonical
        assert "bot" in actors           # canonical

    def test_each_source_color_is_defined(self):
        """All 5 canonical sources have a color in _SOURCE_COLOR_MAP."""
        for deploy, role, _ in pd._MULTI_SOURCE_DEFS:
            assert deploy in pd._SOURCE_COLOR_MAP, (
                f"{deploy} missing from _SOURCE_COLOR_MAP"
            )
