"""AC2/AC12/AC13 — stages.py injeta log_reaper_block e log_reaper_unblock
em _reap_one (issue #558).

AC2: linhas caplog satisfazem regex canônica de formato.
AC12: cenário block (attempt=2 → next=3 == cap=3).
AC13: cenário unblock (attempt=1 → next=2 < cap=3).
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor
from deile.orchestration.pipeline.stages import reap_orphan_claims

_CANONICAL = re.compile(r"^reaper\.[a-z_]+  ([a-z_]+=('[^']*'|[^ ]+) ?)+$")


# ---------------------------------------------------------------------------
# Helpers (mirrors test_reaper.py)
# ---------------------------------------------------------------------------


def _make_pr(number, *, labels, head_ref="auto/issue-1"):
    pr = MagicMock()
    pr.number = number
    pr.labels = list(labels)
    pr.head_ref = head_ref
    pr.is_draft = False
    pr.url = f"https://github.com/o/r/pull/{number}"
    pr.title = f"PR #{number}"
    pr.batch_id = next(
        (lb[len("~batch:") :] for lb in labels if lb.startswith("~batch:")),
        None,
    )
    return pr


def _make_monitor_for_reaper(*, reaper_stale_seconds=60, reaper_max_attempts=3):
    cfg = PipelineConfig(
        repo="owner/repo",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        use_pid_lock=False,
        reaper_stale_seconds=reaper_stale_seconds,
        reaper_max_attempts=reaper_max_attempts,
    )
    github = MagicMock()
    github.list_open_prs = AsyncMock(return_value=[])
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.label_applied_at = AsyncMock(return_value=None)
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.comment_on_issue = AsyncMock()
    notifier = MagicMock()
    notifier.reaper_blocked = AsyncMock()
    worktrees = MagicMock()
    claude = MagicMock()
    monitor = PipelineMonitor(
        cfg,
        github=github,
        worktrees=worktrees,
        claude=claude,
        notifier=notifier,
    )
    return monitor, github


@pytest.fixture(autouse=True)
def _reset_dedup(monkeypatch):
    fresh = pl._DedupCache()
    monkeypatch.setattr(pl, "_DEDUP", fresh)


# ---------------------------------------------------------------------------
# AC2 — formato canônico de ambas as famílias
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_reaper_block_format(caplog):
    """reaper.block emite linha que satisfaz regex canônica."""
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=60,
        reaper_max_attempts=3,
    )
    own = monitor.identity.ownership_label()
    pr = _make_pr(200, labels=["~review:em_andamento", "~batch:def", own, "~attempt:2"])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        await reap_orphan_claims(monitor)

    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    block_lines = [l for l in lines if l.startswith("reaper.block  ")]
    assert block_lines, f"No reaper.block line emitted. All lines: {lines}"
    assert _CANONICAL.match(block_lines[0]), repr(block_lines[0])


@pytest.mark.asyncio
async def test_ac2_reaper_unblock_format(caplog):
    """reaper.unblock emite linha que satisfaz regex canônica."""
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=60,
        reaper_max_attempts=3,
    )
    own = monitor.identity.ownership_label()
    pr = _make_pr(200, labels=["~review:em_andamento", "~batch:def", own, "~attempt:1"])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        await reap_orphan_claims(monitor)

    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    unblock_lines = [l for l in lines if l.startswith("reaper.unblock  ")]
    assert unblock_lines, f"No reaper.unblock line emitted. All lines: {lines}"
    assert _CANONICAL.match(unblock_lines[0]), repr(unblock_lines[0])


# ---------------------------------------------------------------------------
# AC12 — block scenario (attempt=2 → next=3 == cap=3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac12_reaper_block_at_cap(caplog):
    """PR com ~attempt:2, cap=3 → next_attempt=3 == cap → ramo block.

    Verifica: exatamente 1 linha reaper.block com attempts=3, cap=3 e
    reason regex '(PR|issue) #\\d+ \\w+ stuck há \\d+min'.
    Exemplo concreto: reaper.block  target_kind=pr target=200 attempts=3 cap=3 reason='PR #200 review stuck há 3min'
    """
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=60,
        reaper_max_attempts=3,
    )
    own = monitor.identity.ownership_label()
    pr = _make_pr(200, labels=["~review:em_andamento", "~batch:def", own, "~attempt:2"])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    with caplog.at_level(logging.WARNING, logger="deile.pipeline.events"):
        await reap_orphan_claims(monitor)

    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    block_lines = [l for l in lines if l.startswith("reaper.block  ")]
    assert len(block_lines) == 1, f"Expected 1 reaper.block line, got {block_lines}"

    line = block_lines[0]
    assert "attempts=3" in line, repr(line)
    assert "cap=3" in line, repr(line)
    assert "target_kind=pr" in line, repr(line)
    assert "target=200" in line, repr(line)
    assert re.search(r"reason='(PR|issue) #\d+ \w+ stuck há \d+min'", line), repr(line)


# ---------------------------------------------------------------------------
# AC13 — unblock scenario (attempt=1 → next=2 < cap=3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac13_reaper_unblock_before_cap(caplog):
    """PR com ~attempt:1, cap=3 → next_attempt=2 < cap → ramo unblock.

    Verifica: exatamente 1 linha reaper.unblock com attempts=2,
    last_activity_s=200 e reason regex '(PR|issue) #\\d+ \\w+ stuck há \\d+min'.
    Exemplo concreto: reaper.unblock  target_kind=pr target=200 attempts=2 reason='PR #200 review stuck há 3min' last_activity_s=200
    """
    monitor, github = _make_monitor_for_reaper(
        reaper_stale_seconds=60,
        reaper_max_attempts=3,
    )
    own = monitor.identity.ownership_label()
    pr = _make_pr(200, labels=["~review:em_andamento", "~batch:def", own, "~attempt:1"])
    github.list_open_prs = AsyncMock(return_value=[pr])
    github.label_applied_at = AsyncMock(return_value=int(time.time()) - 200)

    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        await reap_orphan_claims(monitor)

    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    unblock_lines = [l for l in lines if l.startswith("reaper.unblock  ")]
    assert (
        len(unblock_lines) == 1
    ), f"Expected 1 reaper.unblock line, got {unblock_lines}"

    line = unblock_lines[0]
    assert "attempts=2" in line, repr(line)
    assert "last_activity_s=200" in line, repr(line)
    assert "target_kind=pr" in line, repr(line)
    assert "target=200" in line, repr(line)
    assert re.search(r"reason='(PR|issue) #\d+ \w+ stuck há \d+min'", line), repr(line)
