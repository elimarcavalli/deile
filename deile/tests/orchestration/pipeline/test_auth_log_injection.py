"""AC2/AC10 — auth log injection in stages.py (issue #559).

Tests that record_auth_failure_and_maybe_pause and the auth-skip paths emit
the 4 structured log families: auth.fail, auth.backoff, auth.skip, auth.recover.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl
from deile.orchestration.pipeline.pipeline_logger import log_auth_recover, log_auth_skip
from deile.orchestration.pipeline.stages import (
    _AUTH_BACKOFF_THRESHOLD,
    _auth_target_key,
    record_auth_failure_and_maybe_pause,
)


def _make_monitor():
    m = MagicMock()
    m._auth_failures_by_target = {}
    m._paused_until_ts = {}
    return m


def _events(caplog):
    return [r.message for r in caplog.records if r.name == "deile.pipeline.events"]


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    monkeypatch.setattr(pl, "_DEDUP", pl._DedupCache())


def test_log_auth_fail_pattern(caplog):
    mon = _make_monitor()
    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        record_auth_failure_and_maybe_pause(mon, "issue", 1)
    lines = _events(caplog)
    assert lines, "No log line emitted for auth.fail"
    pattern = r"auth\.fail\s+target=\S+\s+attempts=\d+\s+threshold=3\s+reason=WORKER_AUTH_EXPIRED"
    assert any(re.search(pattern, line) for line in lines), f"No match in: {lines}"


def test_log_auth_backoff_pattern(caplog):
    mon = _make_monitor()
    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        for _ in range(_AUTH_BACKOFF_THRESHOLD):
            record_auth_failure_and_maybe_pause(mon, "issue", 42)
    lines = _events(caplog)
    pattern = r"auth\.backoff\s+target=\S+\s+attempts=\d+\s+until_iso=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+backoff_s=\d+"
    assert any(re.search(pattern, line) for line in lines), f"No auth.backoff match in: {lines}"


def test_log_auth_skip_pattern(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        log_auth_skip(target="issue:99", until_iso="2023-11-14T22:21:20Z", remaining_s=480)
    lines = _events(caplog)
    assert lines, "No log line emitted for auth.skip"
    pattern = r"auth\.skip\s+target=\S+\s+until_iso=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s+remaining_s=\d+"
    assert any(re.search(pattern, line) for line in lines), f"No match in: {lines}"


def test_log_auth_recover_pattern(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        log_auth_recover(target="issue:1", reason="success")
    lines = _events(caplog)
    assert lines, "No log line emitted for auth.recover"
    pattern = r"auth\.recover\s+target=\S+\s+reason=\S+"
    assert any(re.search(pattern, line) for line in lines), f"No match in: {lines}"


def test_auth_backoff_concrete_values(monkeypatch, caplog):
    """AC10 — concrete values with mocked _monotonic and now_utc."""
    import deile.orchestration.pipeline.stages as stages_mod

    monkeypatch.setattr(stages_mod, "_monotonic", lambda: 0.0)
    fixed_now = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(stages_mod, "now_utc", lambda: fixed_now)

    mon = _make_monitor()
    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        for _ in range(_AUTH_BACKOFF_THRESHOLD):
            record_auth_failure_and_maybe_pause(mon, "issue", 7)

    lines = _events(caplog)
    # count=3, backoff_s = min(60 * 2**3, 1800) = min(480, 1800) = 480
    # until_iso = now_utc + 480s = 2023-11-14T22:13:20Z + 480s = 2023-11-14T22:21:20Z
    backoff_lines = [line for line in lines if "auth.backoff" in line]
    assert backoff_lines, f"No auth.backoff line in: {lines}"
    line = backoff_lines[0]
    assert "backoff_s=480" in line, f"Expected backoff_s=480 in: {line!r}"
    assert "until_iso=2023-11-14T22:21:20Z" in line, f"Expected until_iso=2023-11-14T22:21:20Z in: {line!r}"
