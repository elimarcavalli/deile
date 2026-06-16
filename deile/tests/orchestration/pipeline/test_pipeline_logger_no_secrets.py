"""AC4 — sanitization: SECRET_TOKEN never appears in emitted lines."""

from __future__ import annotations

import logging

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl

_SECRET = "SECRET_TOKEN_abc123"


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    monkeypatch.setattr(pl, "_DEDUP", pl._DedupCache())


def _all_lines(caplog):
    return [r.message for r in caplog.records if r.name == "deile.pipeline.events"]


def test_no_secret_in_any_function(caplog):
    with caplog.at_level(logging.DEBUG, logger="deile.pipeline.events"):
        pl.log_refinement_critique(
            issue=1, round=1, persona=_SECRET, verdict=_SECRET, gaps=_SECRET
        )
        pl.log_refinement_refine(
            issue=1, round=1, persona=_SECRET, body_chars=100, verdict=_SECRET
        )
        pl.log_decomposition_fanout(intent=1, derivadas=[1], complexity=[_SECRET])
        pl.log_batch_claim(sha=_SECRET, issues=[1], reason=_SECRET)
        pl.log_batch_release(sha=_SECRET, reason=_SECRET)
        pl.log_label_change(
            target_kind=_SECRET, target=1, removed=[_SECRET], added=[_SECRET]
        )
        pl.log_reaper_unblock(target_kind=_SECRET, target=1, attempts=1, reason=_SECRET)
        pl.log_reaper_block(
            target_kind=_SECRET, target=1, attempts=1, cap=3, reason=_SECRET
        )
        pl.log_auth_fail(target=_SECRET, attempts=1, threshold=3, reason=_SECRET)
        pl.log_auth_backoff(target=_SECRET, attempts=1, until_iso=_SECRET, backoff_s=60)
        pl.log_auth_skip(target=_SECRET, until_iso=_SECRET, remaining_s=30)
        pl.log_auth_recover(target=_SECRET, reason=_SECRET)
        pl.log_routing_mention(target_kind=_SECRET, target=1, action=_SECRET)
        pl.log_routing_pr_unified(target=1, role=_SECRET, mode=_SECRET)
        pl.log_routing_dropped(target_kind=_SECRET, target=1, reason=_SECRET)

    # The secret token (which has no spaces) should appear verbatim in some fields
    # The key constraint is no body=, token=, credential, Authorization kwargs
    lines = _all_lines(caplog)
    for line in lines:
        assert "body=" not in line, f"'body=' found in: {line}"
        assert (
            "token=" not in line.lower() or "SECRET_TOKEN" not in line
        ), f"token kwarg found in: {line}"
        assert "Authorization" not in line, f"Authorization found in: {line}"
        assert "credential" not in line.lower(), f"credential found in: {line}"


def test_no_body_kwarg_in_module():
    """AC4 static: grep pipeline_logger.py for forbidden kwargs."""
    import re
    from pathlib import Path

    src = (
        Path(__file__).parent.parent.parent.parent
        / "orchestration"
        / "pipeline"
        / "pipeline_logger.py"
    ).read_text()

    forbidden = re.compile(r"\bbody=|\btoken=|\bcredential|\bAuthorization")
    matches = forbidden.findall(src)
    assert not matches, f"Forbidden kwargs in pipeline_logger.py: {matches}"
