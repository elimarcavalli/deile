"""AC6 — schema constraints: line length, no control chars, truncation."""

from __future__ import annotations

import logging

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    monkeypatch.setattr(pl, "_DEDUP", pl._DedupCache())


def _lines(caplog):
    return [r.message for r in caplog.records if r.name == "deile.pipeline.events"]


def _emit_all(caplog):
    with caplog.at_level(logging.DEBUG, logger="deile.pipeline.events"):
        pl.log_refinement_critique(issue=1, round=1, persona="P", verdict="V", gaps="g")
        pl.log_refinement_refine(
            issue=1, round=1, persona="P", body_chars=100, verdict="V"
        )
        pl.log_decomposition_fanout(intent=1, derivadas=[2, 3], complexity=["S"])
        pl.log_batch_claim(sha="s", issues=[1], reason="r")
        pl.log_batch_release(sha="s", reason="r")
        pl.log_label_change(target_kind="issue", target=1, removed=["a"], added=["b"])
        pl.log_reaper_unblock(target_kind="issue", target=1, attempts=1, reason="r")
        pl.log_reaper_block(
            target_kind="issue", target=1, attempts=3, cap=3, reason="r"
        )
        pl.log_auth_fail(target="t", attempts=1, threshold=3, reason="r")
        pl.log_auth_backoff(target="t", attempts=3, until_iso="2026T", backoff_s=60)
        pl.log_auth_skip(target="t", until_iso="2026T", remaining_s=10)
        pl.log_auth_recover(target="t", reason="success")
        pl.log_routing_mention(target_kind="issue", target=1, action="comment")
        pl.log_routing_pr_unified(target=1, role="author", mode="pr_unified")
        pl.log_routing_dropped(target_kind="issue", target=1, reason="self_mention")
    return _lines(caplog)


def test_all_lines_max_500_chars(caplog):
    lines = _emit_all(caplog)
    assert lines, "No lines emitted"
    for line in lines:
        assert len(line) <= 500, f"Line too long ({len(line)}): {line!r}"


def test_no_control_chars(caplog):
    lines = _emit_all(caplog)
    for line in lines:
        assert "\n" not in line, f"newline in: {line!r}"
        assert "\t" not in line, f"tab in: {line!r}"
        assert "\r" not in line, f"CR in: {line!r}"


def test_reason_truncated_at_200(caplog):
    long_reason = "x" * 250
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_dropped(target_kind="issue", target=1, reason=long_reason)
    lines = _lines(caplog)
    assert lines
    line = lines[0]
    assert "..." in line
    assert len(line) <= 500


def test_gaps_truncated_at_200(caplog):
    long_gaps = "g" * 250
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_refinement_critique(
            issue=1, round=1, persona="P", verdict="V", gaps=long_gaps
        )
    lines = _lines(caplog)
    assert lines
    line = lines[0]
    assert "..." in line


def test_control_chars_stripped_from_input(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_refinement_critique(
            issue=1, round=1, persona="P", verdict="V", gaps="a\nb\tc"
        )
    lines = _lines(caplog)
    assert lines
    line = lines[0]
    assert "\n" not in line
    assert "\t" not in line
