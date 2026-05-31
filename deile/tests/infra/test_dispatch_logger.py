"""Tests for ``infra/k8s/dispatch_logger.py`` (issue #435).

Covers:
1. ``log_health_probe`` throttle (≤1/30s per path).
2. Each ``dispatch.*``, ``git.*``, ``forge.*`` emitter: correct event name,
   required keys present, optional keys omitted when None.
3. ``_panel_data`` regex backward-compat: both ``dispatch_started`` /
   ``dispatch.received`` parse to the same CurrentTask; both
   ``dispatch_completed`` / ``dispatch.completed`` clear it;
   ``dispatch.failed`` also clears it as a terminal event.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Add infra/k8s to sys.path so dispatch_logger and _panel_data can be imported.
_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dispatch_logger as dlog  # noqa: E402
import _panel_data as pd  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_records(logger_name: str):
    """Context manager that captures log records emitted to *logger_name*."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        records: list[logging.LogRecord] = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record)

        h = _H()
        lg = logging.getLogger(logger_name)
        lg.addHandler(h)
        old_level = lg.level
        lg.setLevel(logging.DEBUG)
        try:
            yield records
        finally:
            lg.removeHandler(h)
            lg.setLevel(old_level)

    return _ctx()


def _ts_now(offset_s: int = 0) -> str:
    from datetime import timedelta
    ts = datetime.now(timezone.utc) - timedelta(seconds=offset_s)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000000000+00:00")


# ---------------------------------------------------------------------------
# Health-probe throttle
# ---------------------------------------------------------------------------

class TestHealthProbeThrottle:
    def setup_method(self):
        # Reset throttle state between tests.
        dlog._probe_last.clear()

    def test_first_probe_is_logged(self):
        with _capture_records("deile.dispatch") as records:
            dlog.log_health_probe("/v1/health", 200)
        assert len(records) == 1
        assert records[0].getMessage().startswith("health.probe")
        assert "path=/v1/health" in records[0].getMessage()
        assert "status=200" in records[0].getMessage()

    def test_second_probe_within_window_is_suppressed(self):
        dlog.log_health_probe("/v1/health", 200)
        with _capture_records("deile.dispatch") as records:
            dlog.log_health_probe("/v1/health", 200)
        assert len(records) == 0

    def test_probe_after_window_is_logged_again(self):
        dlog.log_health_probe("/v1/health", 200)
        # Force timestamp to be in the past so the window has expired.
        dlog._probe_last["/v1/health"] = time.monotonic() - dlog._PROBE_THROTTLE_S - 1
        with _capture_records("deile.dispatch") as records:
            dlog.log_health_probe("/v1/health", 200)
        assert len(records) == 1

    def test_different_paths_throttled_independently(self):
        with _capture_records("deile.dispatch") as records:
            dlog.log_health_probe("/v1/health", 200)
            dlog.log_health_probe("/v1/pod-status", 200)
        # Two different paths → both logged.
        assert len(records) == 2

    def test_same_path_suppressed_after_first(self):
        dlog.log_health_probe("/v1/health", 200)
        with _capture_records("deile.dispatch") as records:
            dlog.log_health_probe("/v1/health", 200)
            dlog.log_health_probe("/v1/health", 200)
        assert len(records) == 0


# ---------------------------------------------------------------------------
# dispatch_received
# ---------------------------------------------------------------------------

class TestDispatchReceived:
    def test_emits_correct_event_name(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(task="aabbccddeeff1122", channel="pipeline-issue-309")
        assert len(records) == 1
        msg = records[0].getMessage()
        assert msg.startswith("dispatch.received ")

    def test_required_keys_present(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(task="aabbccddeeff1122", channel="pipeline-issue-309")
        msg = records[0].getMessage()
        assert "task=aabbccddeeff1122" in msg
        assert "channel=pipeline-issue-309" in msg

    def test_optional_keys_emitted_when_set(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(
                task="aabbccddeeff1122",
                channel="pipeline-issue-309",
                stage="implement",
                issue=309,
                kind="implement",
                branch="auto/issue-309",
                model_requested="anthropic:claude-opus-4-8",
            )
        msg = records[0].getMessage()
        assert "stage=implement" in msg
        assert "issue=309" in msg
        assert "kind=implement" in msg
        assert "branch=auto/issue-309" in msg
        assert "model_requested=anthropic:claude-opus-4-8" in msg

    def test_none_values_omitted(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(
                task="aabbccddeeff1122",
                channel="ch",
                stage=None,
                issue=None,
            )
        msg = records[0].getMessage()
        assert "stage=" not in msg
        assert "issue=" not in msg
        assert "None" not in msg


# ---------------------------------------------------------------------------
# dispatch_completed
# ---------------------------------------------------------------------------

class TestDispatchCompleted:
    def test_emits_correct_event(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_completed(task="aabb1122", ok=True)
        msg = records[0].getMessage()
        assert msg.startswith("dispatch.completed ")
        assert "task=aabb1122" in msg
        assert "ok=True" in msg

    def test_optional_enrichment(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_completed(
                task="aabb1122", ok=True,
                turns=5, cost_usd=0.001234, duration_s=120.5,
            )
        msg = records[0].getMessage()
        assert "turns=5" in msg
        assert "cost_usd=0.001234" in msg
        assert "duration_s=120.5" in msg


# ---------------------------------------------------------------------------
# dispatch_failed
# ---------------------------------------------------------------------------

class TestDispatchFailed:
    def test_emits_correct_event(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_failed(task="aabb1122", reason="outer_timeout", error_code="TASK_TIMEOUT")
        msg = records[0].getMessage()
        assert msg.startswith("dispatch.failed ")
        assert "task=aabb1122" in msg
        assert "reason=outer_timeout" in msg
        assert "error_code=TASK_TIMEOUT" in msg

    def test_none_error_code_omitted(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_failed(task="aabb1122", reason="something")
        msg = records[0].getMessage()
        assert "error_code=" not in msg


# ---------------------------------------------------------------------------
# git / forge emitters — smoke tests
# ---------------------------------------------------------------------------

class TestGitForgeEmitters:
    def test_git_commit(self):
        with _capture_records("deile.dispatch") as records:
            dlog.git_commit(task="t1", sha="abc1234", branch="auto/issue-1")
        assert records[0].getMessage().startswith("git.commit ")

    def test_git_push(self):
        with _capture_records("deile.dispatch") as records:
            dlog.git_push(task="t1", branch="auto/issue-1", sha="abc1234")
        assert records[0].getMessage().startswith("git.push ")

    def test_forge_pr_open(self):
        with _capture_records("deile.dispatch") as records:
            dlog.forge_pr_open(task="t1", pr=42, url="https://github.com/x/y/pull/42")
        assert records[0].getMessage().startswith("forge.pr_open ")
        assert "pr=42" in records[0].getMessage()

    def test_forge_pr_review(self):
        with _capture_records("deile.dispatch") as records:
            dlog.forge_pr_review(task="t1", pr=42, decision="APPROVED")
        assert records[0].getMessage().startswith("forge.pr_review ")
        assert "decision=APPROVED" in records[0].getMessage()

    def test_forge_pr_merge(self):
        with _capture_records("deile.dispatch") as records:
            dlog.forge_pr_merge(task="t1", pr=42, sha="deadbeef")
        assert records[0].getMessage().startswith("forge.pr_merge ")


# ---------------------------------------------------------------------------
# _panel_data regex backward-compatibility (issue #435)
# ---------------------------------------------------------------------------

class TestPanelDataRegexCompat:
    """WorkerProvider._parse must accept BOTH old (snake) and new (dot) formats."""

    def _build(self) -> "pd.WorkerProvider":
        prov = pd.WorkerProvider(ttl_s=0.0)
        prov._kubectl = "kubectl"
        return prov

    # --- dispatch.received (new) ------------------------------------------

    def test_dispatch_received_parsed_as_current_task(self):
        prov = self._build()
        body = ("dispatch.received task=aabbccddeeff1122 channel=pipeline-issue-309 "
                "stage=implement kind=implement issue=309 branch=auto/issue-309")
        text = f"{_ts_now(2)} {body}"
        state = prov._parse("worker-1", text)
        assert state.current_task is not None
        assert state.current_task.task_id == "aabbccddeeff1122"
        assert state.current_task.channel_id == "pipeline-issue-309"
        assert state.current_task.stage == "implement"
        assert state.current_task.issue_number == 309

    def test_dispatch_received_cleared_by_dispatch_completed(self):
        prov = self._build()
        text = "\n".join([
            f"{_ts_now(5)} dispatch.received task=aabbccddeeff1122 channel=pipeline-issue-309 issue=309",
            f"{_ts_now(2)} dispatch.completed task=aabbccddeeff1122 ok=True",
        ])
        state = prov._parse("worker-1", text)
        assert state.current_task is None
        assert state.last_completed is not None
        assert state.last_completed.outcome == "DONE"

    def test_dispatch_received_cleared_by_dispatch_failed(self):
        prov = self._build()
        text = "\n".join([
            f"{_ts_now(5)} dispatch.received task=aabbccddeeff1122 channel=pipeline-issue-309 issue=309",
            f"{_ts_now(2)} dispatch.failed task=aabbccddeeff1122 reason=outer_timeout error_code=TASK_TIMEOUT",
        ])
        state = prov._parse("worker-1", text)
        assert state.current_task is None
        assert state.last_completed is not None
        assert state.last_completed.outcome == "FAIL"

    # --- dispatch_started (legacy) still works --------------------------------

    def test_legacy_dispatch_started_still_parsed(self):
        prov = self._build()
        body = ("dispatch_started task=aabbccddeeff1122 channel=pipeline-issue-309 "
                "stage=implement kind=implement issue=309")
        text = f"{_ts_now(2)} {body}"
        state = prov._parse("worker-1", text)
        assert state.current_task is not None
        assert state.current_task.task_id == "aabbccddeeff1122"

    def test_legacy_dispatch_completed_still_clears(self):
        prov = self._build()
        text = "\n".join([
            f"{_ts_now(5)} dispatch_started task=aabbccddeeff1122 channel=pipeline-issue-309 issue=309",
            f"{_ts_now(2)} dispatch_completed task=aabbccddeeff1122 ok=True",
        ])
        state = prov._parse("worker-1", text)
        assert state.current_task is None

    # --- mixed: old started, new completed (rolling deploy) -------------------

    def test_old_started_new_completed_pairs_correctly(self):
        prov = self._build()
        text = "\n".join([
            f"{_ts_now(5)} dispatch_started task=aabbccddeeff1122 channel=pipeline-issue-309 issue=309",
            f"{_ts_now(2)} dispatch.completed task=aabbccddeeff1122 ok=True",
        ])
        state = prov._parse("worker-1", text)
        assert state.current_task is None

    def test_new_started_old_completed_pairs_correctly(self):
        prov = self._build()
        text = "\n".join([
            f"{_ts_now(5)} dispatch.received task=aabbccddeeff1122 channel=pipeline-issue-309 issue=309",
            f"{_ts_now(2)} dispatch_completed task=aabbccddeeff1122 ok=True",
        ])
        state = prov._parse("worker-1", text)
        assert state.current_task is None

    # --- dispatch.failed for old-format started task -------------------------

    def test_dispatch_failed_clears_legacy_started_task(self):
        prov = self._build()
        text = "\n".join([
            f"{_ts_now(5)} dispatch_started task=aabbccddeeff1122 channel=pipeline-issue-309 issue=309",
            f"{_ts_now(2)} dispatch.failed task=aabbccddeeff1122 reason=cancelled",
        ])
        state = prov._parse("worker-1", text)
        assert state.current_task is None
        assert state.last_completed is not None
        assert state.last_completed.outcome == "FAIL"
