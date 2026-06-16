"""AC16 — failure isolation: all 15 functions never propagate exceptions."""

from __future__ import annotations

import logging

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl

_ALL_CALLS = [
    (pl.log_refinement_critique, dict(issue=1, round=1, persona="P", verdict="V")),
    (
        pl.log_refinement_refine,
        dict(issue=1, round=1, persona="P", body_chars=10, verdict="V"),
    ),
    (pl.log_decomposition_fanout, dict(intent=1, derivadas=[2], complexity=["S"])),
    (pl.log_batch_claim, dict(sha="s", issues=[1], reason="r")),
    (pl.log_batch_release, dict(sha="s", reason="r")),
    (pl.log_label_change, dict(target_kind="issue", target=1, removed=[], added=["a"])),
    (
        pl.log_reaper_unblock,
        dict(target_kind="issue", target=1, attempts=1, reason="r"),
    ),
    (
        pl.log_reaper_block,
        dict(target_kind="issue", target=1, attempts=3, cap=3, reason="r"),
    ),
    (pl.log_auth_fail, dict(target="t", attempts=1, threshold=3, reason="err")),
    (
        pl.log_auth_backoff,
        dict(target="t", attempts=3, until_iso="2026T", backoff_s=60),
    ),
    (pl.log_auth_skip, dict(target="t", until_iso="2026T", remaining_s=10)),
    (pl.log_auth_recover, dict(target="t", reason="success")),
    (pl.log_routing_mention, dict(target_kind="issue", target=1, action="comment")),
    (pl.log_routing_pr_unified, dict(target=1, role="author", mode="pr_unified")),
    (
        pl.log_routing_dropped,
        dict(target_kind="issue", target=1, reason="self_mention"),
    ),
]


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    monkeypatch.setattr(pl, "_DEDUP", pl._DedupCache())


def test_all_functions_do_not_raise_when_emit_fails(monkeypatch):
    """Force _LOG.handle to raise; every function must return None silently."""
    logger = logging.getLogger("deile.pipeline.events")

    def _boom(record):
        raise RuntimeError("forced emit failure")

    monkeypatch.setattr(logger, "handle", _boom)

    for func, kwargs in _ALL_CALLS:
        result = func(**kwargs)
        assert result is None, f"{func.__name__} returned {result!r} instead of None"


def test_all_functions_do_not_raise_when_formatting_fails(monkeypatch):
    """Patching _build_line to raise; still must not propagate."""
    monkeypatch.setattr(
        pl,
        "_build_line",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("fmt fail")),
    )

    for func, kwargs in _ALL_CALLS:
        result = func(**kwargs)
        assert result is None, f"{func.__name__} should return None on fmt error"


def test_return_value_is_none():
    """Normal operation: all functions return None."""
    for func, kwargs in _ALL_CALLS:
        result = func(**kwargs)
        assert result is None, f"{func.__name__} returned {result!r}"
