"""AC8 + AC2 — batch.claim / batch.release logging via pipeline_logger."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl
from deile.orchestration.pipeline import stages
from deile.orchestration.pipeline.identity import MonitorIdentity
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor

_CANON_PATTERN = re.compile(
    r"^(refinement|decomposition|batch|label|reaper|auth|routing)\.[a-z_]+"
    r"  ([a-z_]+=('[^']*'|[^ ]+) ?)+$"
)


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    monkeypatch.setattr(pl, "_DEDUP", pl._DedupCache())


def _batch_lines(caplog):
    return [
        r.message for r in caplog.records
        if r.name == "deile.pipeline.events" and r.message.startswith("batch.")
    ]


def _make_monitor(shard_count: int, claim_returns: object = "abc12345"):
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
    )
    forge = MagicMock()
    forge.ensure_pipeline_labels = AsyncMock()
    forge.claim_with_batch = AsyncMock(return_value=claim_returns)
    forge.clear_batch_label = AsyncMock()
    forge.add_labels = AsyncMock()
    forge.remove_labels = AsyncMock()
    forge.on_label_change = None

    monitor = PipelineMonitor(
        cfg,
        forge=forge,
        worktrees=MagicMock(),
        claude=MagicMock(),
    )
    monitor.identity = MonitorIdentity(
        monitor_id="default", shard_index=0, shard_count=shard_count,
    )
    return monitor


class TestBatchLoggingAC8:
    """AC8 — 0 batch.* lines for shard_count=1, exactly 2 for shard_count=2."""

    async def test_single_monitor_emits_no_batch_lines(self, caplog):
        monitor = _make_monitor(shard_count=1)
        with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
            await stages._claim_for_classify(monitor, "issue", 42, error_context="test")
            await stages._release_classify_claim(monitor, "issue", 42)
        lines = _batch_lines(caplog)
        assert lines == [], f"shard_count=1 must emit 0 batch.* lines, got: {lines}"

    async def test_multi_monitor_emits_exactly_two_batch_lines(self, caplog):
        monitor = _make_monitor(shard_count=2, claim_returns="abc12345")
        with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
            claimed = await stages._claim_for_classify(
                monitor, "issue", 42, error_context="test_context",
            )
            await stages._release_classify_claim(monitor, "issue", 42)
        assert claimed is True
        lines = _batch_lines(caplog)
        assert len(lines) == 2, (
            f"shard_count=2 must emit exactly 2 batch.* lines, got: {lines}"
        )
        assert any(ln.startswith("batch.claim") for ln in lines)
        assert any(ln.startswith("batch.release") for ln in lines)

    async def test_claim_returns_none_emits_no_batch_lines(self, caplog):
        monitor = _make_monitor(shard_count=2, claim_returns=None)
        with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
            claimed = await stages._claim_for_classify(monitor, "issue", 42, error_context="test")
        assert claimed is False
        lines = _batch_lines(caplog)
        assert lines == [], f"claim=None must emit 0 batch.* lines, got: {lines}"


class TestBatchLoggingAC2:
    """AC2 (partial) — batch.* lines match canonical format regex."""

    async def test_batch_claim_matches_canonical_pattern(self, caplog):
        monitor = _make_monitor(shard_count=2, claim_returns="abc12345")
        with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
            await stages._claim_for_classify(
                monitor, "issue", 42, error_context="classify",
            )
        lines = _batch_lines(caplog)
        claim_lines = [ln for ln in lines if ln.startswith("batch.claim")]
        assert claim_lines, "No batch.claim line emitted"
        for line in claim_lines:
            assert _CANON_PATTERN.match(line), (
                f"batch.claim does not match canonical pattern: {line!r}"
            )

    async def test_batch_release_matches_canonical_pattern(self, caplog):
        monitor = _make_monitor(shard_count=2, claim_returns="abc12345")
        with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
            await stages._claim_for_classify(monitor, "issue", 42, error_context="classify")
            await stages._release_classify_claim(monitor, "issue", 42)
        lines = _batch_lines(caplog)
        release_lines = [ln for ln in lines if ln.startswith("batch.release")]
        assert release_lines, "No batch.release line emitted"
        for line in release_lines:
            assert _CANON_PATTERN.match(line), (
                f"batch.release does not match canonical pattern: {line!r}"
            )
