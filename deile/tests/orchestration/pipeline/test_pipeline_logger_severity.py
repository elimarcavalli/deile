"""AC7 — severity: 12 INFO events + 3 WARNING events (named)."""

from __future__ import annotations

import logging

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    monkeypatch.setattr(pl, "_DEDUP", pl._DedupCache())


def _emit_one(func, **kw):
    """Emit and return (levelname, message) pairs."""

    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler()
    logger = logging.getLogger("deile.pipeline.events")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        func(**kw)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    return records


def test_refinement_critique_info():
    recs = _emit_one(
        pl.log_refinement_critique, issue=1, round=1, persona="P", verdict="V"
    )
    assert recs and recs[0].levelname == "INFO"


def test_refinement_refine_info():
    recs = _emit_one(
        pl.log_refinement_refine,
        issue=1,
        round=1,
        persona="P",
        body_chars=10,
        verdict="V",
    )
    assert recs and recs[0].levelname == "INFO"


def test_decomposition_fanout_info():
    recs = _emit_one(
        pl.log_decomposition_fanout, intent=1, derivadas=[2], complexity=["S"]
    )
    assert recs and recs[0].levelname == "INFO"


def test_batch_claim_info():
    recs = _emit_one(pl.log_batch_claim, sha="s", issues=[1], reason="r")
    assert recs and recs[0].levelname == "INFO"


def test_batch_release_info():
    recs = _emit_one(pl.log_batch_release, sha="s", reason="r")
    assert recs and recs[0].levelname == "INFO"


def test_label_change_info():
    recs = _emit_one(
        pl.log_label_change, target_kind="issue", target=1, removed=[], added=["a"]
    )
    assert recs and recs[0].levelname == "INFO"


def test_reaper_unblock_info():
    recs = _emit_one(
        pl.log_reaper_unblock, target_kind="issue", target=1, attempts=1, reason="r"
    )
    assert recs and recs[0].levelname == "INFO"


def test_auth_skip_info():
    recs = _emit_one(pl.log_auth_skip, target="t", until_iso="2026T", remaining_s=10)
    assert recs and recs[0].levelname == "INFO"


def test_auth_recover_info():
    recs = _emit_one(pl.log_auth_recover, target="t", reason="success")
    assert recs and recs[0].levelname == "INFO"


def test_routing_mention_info():
    recs = _emit_one(
        pl.log_routing_mention, target_kind="issue", target=1, action="comment"
    )
    assert recs and recs[0].levelname == "INFO"


def test_routing_pr_unified_info():
    recs = _emit_one(
        pl.log_routing_pr_unified, target=1, role="author", mode="pr_unified"
    )
    assert recs and recs[0].levelname == "INFO"


def test_routing_dropped_info():
    recs = _emit_one(
        pl.log_routing_dropped, target_kind="issue", target=1, reason="self_mention"
    )
    assert recs and recs[0].levelname == "INFO"


# 3 WARNING events


def test_reaper_block_warning():
    recs = _emit_one(
        pl.log_reaper_block,
        target_kind="issue",
        target=1,
        attempts=3,
        cap=3,
        reason="r",
    )
    assert recs and recs[0].levelname == "WARNING"


def test_auth_fail_warning():
    recs = _emit_one(
        pl.log_auth_fail,
        target="t",
        attempts=1,
        threshold=3,
        reason="WORKER_AUTH_EXPIRED",
    )
    assert recs and recs[0].levelname == "WARNING"


def test_auth_backoff_warning():
    recs = _emit_one(
        pl.log_auth_backoff, target="t", attempts=3, until_iso="2026T", backoff_s=480
    )
    assert recs and recs[0].levelname == "WARNING"
