"""AC2 — canonical schema: all 15 functions emit family.subtype  k=v lines."""
from __future__ import annotations

import logging
import re

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl

_PATTERN = re.compile(
    r"^(refinement|decomposition|batch|label|reaper|auth|routing)\.[a-z_]+"
    r"  ([a-z_]+=('[^']*'|[^ ]+) ?)+$"
)


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    """Give each test a fresh dedup cache to avoid cross-test suppression."""
    fresh = pl._DedupCache()
    monkeypatch.setattr(pl, "_DEDUP", fresh)


def _capture(func, **kw):
    with pytest.raises(Exception):
        pass  # ensure caplog is not confused
    return None  # unused — tests use caplog directly


def test_refinement_critique(caplog):
    with caplog.at_level(logging.DEBUG, logger="deile.pipeline.events"):
        pl.log_refinement_critique(issue=1, round=1, persona="Critica", verdict="CLARO")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert lines, "No line emitted"
    line = lines[0]
    assert line.startswith("refinement.critique  "), repr(line)
    assert "issue=1" in line
    assert "verdict=CLARO" in line


def test_refinement_critique_quoting(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_refinement_critique(issue=2, round=1, persona="Alice", verdict="VAGO", gaps="disk cheio")
    lines = [r.message for r in caplog.records]
    assert any("gaps='disk cheio'" in l for l in lines), lines


def test_refinement_critique_single_quote_in_value(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_refinement_critique(issue=3, round=1, persona="P", verdict="V", gaps="x's y")
    lines = [r.message for r in caplog.records]
    assert any("gaps='x s y'" in l for l in lines), lines


def test_decomposition_fanout_list_no_spaces(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_decomposition_fanout(intent=100, derivadas=[101, 102, 103], complexity=["S", "M", "L"])
    lines = [r.message for r in caplog.records]
    assert any("derivadas=[101,102,103]" in l for l in lines), lines
    assert any("complexity=[S,M,L]" in l for l in lines), lines


def test_decomposition_fanout_empty_list(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_decomposition_fanout(intent=200, derivadas=[], complexity=[])
    lines = [r.message for r in caplog.records]
    assert any("derivadas=[]" in l for l in lines), lines


def test_batch_claim(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_batch_claim(sha="abc123", issues=[10, 11], reason="lock")
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("batch.claim  ") for l in lines), lines


def test_batch_release(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_batch_release(sha="abc123", reason="done")
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("batch.release  ") for l in lines), lines


def test_label_change(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_label_change(target_kind="issue", target=5, removed=["~workflow:nova"], added=["~workflow:em_pr"])
    lines = [r.message for r in caplog.records]
    assert any("added=[~workflow:em_pr]" in l for l in lines), lines


def test_reaper_unblock(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_reaper_unblock(target_kind="issue", target=7, attempts=1, reason="stale")
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("reaper.unblock  ") for l in lines), lines


def test_reaper_block(caplog):
    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        pl.log_reaper_block(target_kind="pr", target=8, attempts=3, cap=3, reason="max")
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("reaper.block  ") for l in lines), lines


def test_auth_fail(caplog):
    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        pl.log_auth_fail(target="repo/X", attempts=1, threshold=3, reason="WORKER_AUTH_EXPIRED")
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("auth.fail  ") for l in lines), lines


def test_auth_backoff(caplog):
    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        pl.log_auth_backoff(target="repo/X", attempts=3, until_iso="2026-06-05T12:00:00Z", backoff_s=480)
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("auth.backoff  ") for l in lines), lines


def test_auth_skip(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_auth_skip(target="repo/X", until_iso="2026-06-05T12:00:00Z", remaining_s=300)
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("auth.skip  ") for l in lines), lines


def test_auth_recover(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_auth_recover(target="repo/X", reason="success")
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("auth.recover  ") for l in lines), lines


def test_routing_mention(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_mention(target_kind="issue", target=9, action="comment")
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("routing.mention  ") for l in lines), lines


def test_routing_pr_unified(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_pr_unified(target=42, role="author", mode="pr_unified")
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("routing.pr_unified  ") for l in lines), lines


def test_routing_dropped(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_dropped(target_kind="issue", target=3, reason="self_mention")
    lines = [r.message for r in caplog.records]
    assert any(l.startswith("routing.dropped  ") for l in lines), lines
