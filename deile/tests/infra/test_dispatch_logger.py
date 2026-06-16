"""Tests for ``infra/k8s/dispatch_logger.py`` (issue #435).

Covers:
1. ``log_health_probe`` throttle (≤1/30s per path).
2. Each ``dispatch.*``, ``git.*``, ``forge.*`` emitter: correct event name,
   required keys present, optional keys omitted when None.
3. ``_panel_data`` regex: ``dispatch.received`` parses to CurrentTask;
   ``dispatch.completed`` and ``dispatch.failed`` clear it.
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

import _panel_data as pd  # noqa: E402
import dispatch_logger as dlog  # noqa: E402

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
            dlog.dispatch_received(
                task="aabbccddeeff1122", channel="pipeline-issue-309"
            )
        assert len(records) == 1
        msg = records[0].getMessage()
        assert msg.startswith("dispatch.received ")

    def test_required_keys_present(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(
                task="aabbccddeeff1122", channel="pipeline-issue-309"
            )
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
                task="aabb1122",
                ok=True,
                turns=5,
                cost_usd=0.001234,
                duration_s=120.5,
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
            dlog.dispatch_failed(
                task="aabb1122", reason="outer_timeout", error_code="TASK_TIMEOUT"
            )
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
# Phase-2 dispatch emitters — smoke tests (defined, tested; not yet wired)
# ---------------------------------------------------------------------------


class TestDispatchProgressEmitters:
    def test_dispatch_model_resolved(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_model_resolved(
                task="t1", model="anthropic:claude-opus-4-8", source="settings"
            )
        assert records[0].getMessage().startswith("dispatch.model_resolved ")
        assert "model=anthropic:claude-opus-4-8" in records[0].getMessage()

    def test_dispatch_model_resolved_with_reasoning(self):
        """reasoning=<effort> is emitted when the knob is set (issue #441)."""
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_model_resolved(
                task="t2",
                model="anthropic:claude-opus-4-8",
                source="context_data",
                reasoning="high",
            )
        msg = records[0].getMessage()
        assert "reasoning=high" in msg
        assert "source=context_data" in msg

    def test_dispatch_model_resolved_omits_reasoning_when_none(self):
        """reasoning key is absent (not reasoning=None) when effort is not set."""
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_model_resolved(
                task="t3",
                model="anthropic:claude-sonnet-4-6",
                source="settings",
            )
        msg = records[0].getMessage()
        assert "reasoning=" not in msg

    def test_dispatch_progress(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_progress(task="t1", elapsed_s=60.0, turn=3, tool_last="Bash")
        msg = records[0].getMessage()
        assert msg.startswith("dispatch.progress ")
        assert "task=t1" in msg
        assert "elapsed_s=60.0" in msg
        assert "turn=3" in msg
        assert "tool_last=Bash" in msg

    def test_dispatch_tool_burst(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_tool_burst(task="t1", window_s=10.0, tools="Edit:5,Bash:3")
        msg = records[0].getMessage()
        assert msg.startswith("dispatch.tool_burst ")
        assert "tools=Edit:5,Bash:3" in msg


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
        body = (
            "dispatch.received task=aabbccddeeff1122 channel=pipeline-issue-309 "
            "stage=implement kind=implement issue=309 branch=auto/issue-309"
        )
        text = f"{_ts_now(2)} {body}"
        state = prov._parse("worker-1", text)
        assert state.current_task is not None
        assert state.current_task.task_id == "aabbccddeeff1122"
        assert state.current_task.channel_id == "pipeline-issue-309"
        assert state.current_task.stage == "implement"
        assert state.current_task.issue_number == 309

    def test_dispatch_received_cleared_by_dispatch_completed(self):
        prov = self._build()
        text = "\n".join(
            [
                f"{_ts_now(5)} dispatch.received task=aabbccddeeff1122 channel=pipeline-issue-309 issue=309",
                f"{_ts_now(2)} dispatch.completed task=aabbccddeeff1122 ok=True",
            ]
        )
        state = prov._parse("worker-1", text)
        assert state.current_task is None
        assert state.last_completed is not None
        assert state.last_completed.outcome == "DONE"

    def test_dispatch_received_cleared_by_dispatch_failed(self):
        prov = self._build()
        text = "\n".join(
            [
                f"{_ts_now(5)} dispatch.received task=aabbccddeeff1122 channel=pipeline-issue-309 issue=309",
                f"{_ts_now(2)} dispatch.failed task=aabbccddeeff1122 reason=outer_timeout error_code=TASK_TIMEOUT",
            ]
        )
        state = prov._parse("worker-1", text)
        assert state.current_task is None
        assert state.last_completed is not None
        assert state.last_completed.outcome == "FAIL"


# ---------------------------------------------------------------------------
# AC §9b — format integrity: values stay single-token and bounded
# ---------------------------------------------------------------------------


class TestFormatIntegrity:
    def test_newline_in_value_becomes_literal_backslash_n(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(
                task="aabb1122",
                channel="ch",
                branch="feat/with\nnewline",
            )
        msg = records[0].getMessage()
        assert "\n" not in msg.split("\n", 1)[0] or msg.count("\n") == 0
        assert "branch=feat/with\\nnewline" in msg

    def test_carriage_return_in_value_also_normalized(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(
                task="t",
                channel="ch",
                branch="a\rb\r\nc",
            )
        msg = records[0].getMessage()
        assert "\r" not in msg
        assert "branch=a\\nb\\nc" in msg

    def test_whitespace_in_value_collapsed_to_underscore(self):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(
                task="t",
                channel="ch",
                branch="feat/with space and more",
            )
        msg = records[0].getMessage()
        # Parser uses (\w+)=(\S+) so the value must be a single token.
        assert "branch=feat/with_space_and_more" in msg

    def test_value_truncated_to_80_chars(self):
        long = "x" * 200
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(task="t", channel="ch", branch=long)
        msg = records[0].getMessage()
        # Find the branch= segment and check its value length
        seg = [p for p in msg.split() if p.startswith("branch=")][0]
        value = seg.split("=", 1)[1]
        assert len(value) == 80

    def test_parser_kv_regex_survives_sanitized_value(self):
        """The whole point of AC §9b — line stays parseable after weird input."""
        import re as _re

        kv = _re.compile(r"(\w+)=(\S+)")
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(
                task="aabb1122",
                channel="pipeline-issue-309",
                branch="feat/scary\nbranch with spaces",
                issue=309,
            )
        msg = records[0].getMessage()
        kv_pairs = dict(kv.findall(msg))
        # All four keys recovered correctly.
        assert kv_pairs["task"] == "aabb1122"
        assert kv_pairs["channel"] == "pipeline-issue-309"
        assert kv_pairs["issue"] == "309"
        assert "scary" in kv_pairs["branch"]


# ---------------------------------------------------------------------------
# AC §9a — fail-soft: observability never crashes a dispatch
# ---------------------------------------------------------------------------


class TestFailSoft:
    def test_emit_swallows_exceptions_from_logging(self):
        """If the underlying handler raises, _emit must NOT propagate."""
        with patch.object(dlog._logger, "info", side_effect=RuntimeError("boom")):
            # Must not raise.
            dlog.dispatch_received(task="t", channel="ch")
            dlog.dispatch_completed(task="t", ok=True)
            dlog.dispatch_failed(task="t", reason="x")

    def test_emit_swallows_exception_from_str(self):
        class Bad:
            def __str__(self):
                raise RuntimeError("bad __str__")

        # Must not raise.
        dlog._emit("test.event", weird=Bad())


# ---------------------------------------------------------------------------
# AC §8 — redaction: secrets never reach kubectl logs
# ---------------------------------------------------------------------------


class TestRedaction:
    @pytest.mark.parametrize(
        "value,must_not_contain",
        [
            ("Bearer abc123def456ghi789", "abc123def456ghi789"),
            ("token=verysecrettoken12345", "verysecrettoken12345"),
            ("api_key=mysupersecretkey", "mysupersecretkey"),
            ("api-key:abcdefghij1234567890", "abcdefghij1234567890"),
            ("authorization=Bearer xyz", "xyz"),
            (
                "ghp_abcdefghij1234567890klmnopqrst",
                "ghp_abcdefghij1234567890klmnopqrst",
            ),
            ("glpat-abcdefghij1234567890", "glpat-abcdefghij1234567890"),
            ("sk-abcdefghij1234567890abcdef", "sk-abcdefghij1234567890abcdef"),
        ],
    )
    def test_secret_pattern_replaced_with_placeholder(self, value, must_not_contain):
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(task="t", channel="ch", branch=value)
        msg = records[0].getMessage()
        assert must_not_contain not in msg
        assert "<redacted>" in msg

    def test_clean_values_pass_through(self):
        """Innocuous strings must NOT be over-redacted."""
        with _capture_records("deile.dispatch") as records:
            dlog.dispatch_received(
                task="aabb1122",
                channel="pipeline-issue-309",
                branch="auto/issue-309",
                model_requested="anthropic:claude-opus-4-8",
            )
        msg = records[0].getMessage()
        assert "<redacted>" not in msg
        assert "branch=auto/issue-309" in msg
        assert "model_requested=anthropic:claude-opus-4-8" in msg


# ---------------------------------------------------------------------------
# init_logging configures deile.dispatch independently (bug #1)
# ---------------------------------------------------------------------------


class TestDispatchLoggerSurvivesWarningLevel:
    def test_dispatch_lines_emitted_even_when_root_at_warning(
        self, tmp_path, monkeypatch
    ):
        """AC §6 — DEILE_LOG_LEVEL=WARNING must NOT silence dispatch.*"""
        from deile.log_mgmt import init_logging

        # Isolate log dir so this test doesn't pollute the host.
        monkeypatch.setenv("DEILE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("DEILE_POD_NAME", "test-pod")

        # Reset deile.dispatch handlers so each test starts clean.
        lg = logging.getLogger("deile.dispatch")
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.setLevel(logging.NOTSET)

        init_logging(pod_name="test-pod", level="WARNING")

        # After init, deile.dispatch must be at INFO regardless of root level.
        assert lg.level == logging.INFO
        assert lg.propagate is False
        # Has at least one handler attached (idempotent marker).
        assert any(getattr(h, "_deile_dispatch_marker", False) for h in lg.handlers)

        # Calling init_logging again must NOT duplicate the dispatch handler.
        init_logging(pod_name="test-pod", level="WARNING")
        marked = [h for h in lg.handlers if getattr(h, "_deile_dispatch_marker", False)]
        assert len(marked) == 1, f"dispatch handler not idempotent (got {len(marked)})"
