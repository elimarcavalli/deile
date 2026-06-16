"""AC5 — anti-flood dedup: same event within TTL → 1 line only.

i–iii via public functions; iv via _DedupCache standalone with 5001 keys.
"""

from __future__ import annotations

import logging

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    monkeypatch.setattr(pl, "_DEDUP", pl._DedupCache())


def _lines(caplog):
    return [r.message for r in caplog.records if r.name == "deile.pipeline.events"]


def test_label_change_dedup_2x(caplog):
    """AC5(i): same label.change twice in <30s → 1 line."""
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_label_change(target_kind="issue", target=1, removed=["a"], added=["b"])
        pl.log_label_change(target_kind="issue", target=1, removed=["a"], added=["b"])
    assert len(_lines(caplog)) == 1


def test_reaper_unblock_dedup_2x(caplog):
    """AC5(ii): same reaper.unblock (target+attempts) twice in <60s → 1 line."""
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_reaper_unblock(target_kind="issue", target=2, attempts=2, reason="stale")
        pl.log_reaper_unblock(target_kind="issue", target=2, attempts=2, reason="stale")
    assert len(_lines(caplog)) == 1


def test_reaper_block_dedup_2x(caplog):
    """AC5(ii): same reaper.block (target+attempts) twice in <60s → 1 line."""
    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        pl.log_reaper_block(target_kind="pr", target=3, attempts=3, cap=3, reason="max")
        pl.log_reaper_block(target_kind="pr", target=3, attempts=3, cap=3, reason="max")
    assert len(_lines(caplog)) == 1


def test_auth_fail_dedup_3x(caplog):
    """AC5(iii): auth.fail 3× same target in <60s → 1 line."""
    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        for _ in range(3):
            pl.log_auth_fail(target="repo/X", attempts=1, threshold=3, reason="err")
    assert len(_lines(caplog)) == 1


def test_different_targets_not_deduped(caplog):
    """Different targets are independent."""
    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        pl.log_auth_fail(target="repo/A", attempts=1, threshold=3, reason="err")
        pl.log_auth_fail(target="repo/B", attempts=1, threshold=3, reason="err")
    assert len(_lines(caplog)) == 2


def test_dedup_cache_eviction():
    """AC5(iv): _DedupCache standalone, 5001 distinct keys → len <= 2048."""
    from deile.orchestration.pipeline.pipeline_logger import _DedupCache

    cache = _DedupCache()
    for i in range(5001):
        cache.seen_recently(str(i), ttl=60.0)
    assert len(cache) <= 2048, f"Cache size {len(cache)} exceeds 2048"
