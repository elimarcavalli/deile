"""AC2 — canonical schema: all 15 functions emit family.subtype  k=v lines."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import deile.orchestration.pipeline.pipeline_logger as pl
from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor
from deile.orchestration.pipeline.stages import _dispatch_mention_group, MentionTrigger

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


# ---------------------------------------------------------------------------
# AC2 — 9 new routing scenarios (completes 12-scenario minimum from #438)
# ---------------------------------------------------------------------------

def test_routing_mention_inject_workflow_nova(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_mention(target_kind="issue", target=10, action="inject_workflow_nova")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert any("action=inject_workflow_nova" in l for l in lines), lines
    assert any("target_kind=issue" in l for l in lines), lines


def test_routing_mention_already_in_pipeline(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_mention(target_kind="issue", target=11, action="already_in_pipeline")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert any("action=already_in_pipeline" in l for l in lines), lines
    assert any("target_kind=issue" in l for l in lines), lines


def test_routing_pr_unified_requested_reviewer(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_pr_unified(target=20, role="requested_reviewer", mode="pr_unified")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert any("role=requested_reviewer" in l for l in lines), lines
    assert any("mode=pr_unified" in l for l in lines), lines


def test_routing_pr_unified_assignee(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_pr_unified(target=21, role="assignee", mode="pr_unified")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert any("role=assignee" in l for l in lines), lines
    assert any("mode=pr_unified" in l for l in lines), lines


def test_routing_dropped_deferred_active_gate(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_dropped(target_kind="issue", target=30, reason="deferred_active_gate")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert any("reason=deferred_active_gate" in l for l in lines), lines


def test_routing_dropped_issue_human_gated(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_dropped(target_kind="issue", target=31, reason="issue_human_gated")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert any("reason=issue_human_gated" in l for l in lines), lines


def test_routing_dropped_pr_in_review(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_dropped(target_kind="pr", target=32, reason="pr_in_review")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert any("reason=pr_in_review" in l for l in lines), lines


def test_routing_dropped_pr_human_gated(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_dropped(target_kind="pr", target=33, reason="pr_human_gated")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert any("reason=pr_human_gated" in l for l in lines), lines


def test_routing_dropped_attempt_ceiling(caplog):
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        pl.log_routing_dropped(target_kind="pr", target=34, reason="attempt_ceiling")
    lines = [r.message for r in caplog.records if r.name == "deile.pipeline.events"]
    assert any("reason=attempt_ceiling" in l for l in lines), lines


# ---------------------------------------------------------------------------
# AC3 — 3 edge scenarios via _dispatch_mention_group
# ---------------------------------------------------------------------------

def _make_monitor_for_routing(implementer=None):
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        mention_handle="@deile-one",
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.list_open_prs = AsyncMock(return_value=[])
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.list_issues_assigned_to = AsyncMock(return_value=[])
    github.list_prs_assigned_to = AsyncMock(return_value=[])
    github.list_prs_with_review_requests = AsyncMock(return_value=[])
    github.search_items_mentioning = AsyncMock(return_value=([], []))
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.get_issue = AsyncMock(
        return_value=IssueRef(
            number=1, title="t",
            url="https://github.com/o/r/issues/1", labels=(),
        )
    )
    github.get_pr = AsyncMock(return_value=None)

    notifier = MagicMock()
    for attr in (
        "issue_picked_up", "issue_reviewed", "implementation_started",
        "implementation_finished", "implementation_parked", "pr_picked_up",
        "pr_reviewed", "issue_auto_classified", "error", "pr_auto_classified",
        "mention_processed",
    ):
        setattr(notifier, attr, AsyncMock())

    from deile.orchestration.pipeline.implementer import WorkOutcome
    impl = implementer if implementer is not None else MagicMock()
    if isinstance(impl, MagicMock) and implementer is None:
        impl.mention = AsyncMock(return_value=WorkOutcome(ok=True, text="done"))

    monitor = PipelineMonitor(
        cfg, github=github, worktrees=MagicMock(),
        claude=MagicMock(), notifier=notifier, implementer=impl,
    )
    return monitor, github


@pytest.mark.asyncio
async def test_routing_ac3_workflow_waiting_lift_zero_lines(caplog):
    monitor, github = _make_monitor_for_routing()
    issue_num = 100
    github.get_issue = AsyncMock(
        return_value=IssueRef(
            number=issue_num,
            title="t",
            url=f"https://github.com/o/r/issues/{issue_num}",
            labels=("~workflow:aguardando_stakeholder",),
        )
    )
    trigger = MentionTrigger(
        trigger_type="comment",
        issue=IssueRef(
            number=issue_num,
            title="t",
            url=f"https://github.com/o/r/issues/{issue_num}",
            labels=("~workflow:aguardando_stakeholder",),
        ),
    )
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        await _dispatch_mention_group(
            monitor, f"issue:{issue_num}", [trigger], "deile-one", 0.0
        )
    routing_lines = [r.message for r in caplog.records if r.message.startswith("routing.")]
    assert routing_lines == [], f"Expected zero routing.* lines, got: {routing_lines}"


@pytest.mark.asyncio
async def test_routing_ac3_forge_failure_mention_emitted_no_dropped(caplog):
    monitor, github = _make_monitor_for_routing()
    issue_num = 200
    github.add_labels = AsyncMock(side_effect=Exception("forge error"))
    issue = IssueRef(
        number=issue_num,
        title="t",
        url=f"https://github.com/o/r/issues/{issue_num}",
        labels=(),
    )
    trigger = MentionTrigger(trigger_type="assignee", issue=issue)
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        await _dispatch_mention_group(
            monitor, f"issue:{issue_num}", [trigger], "deile-one", 0.0
        )
    routing_lines = [r.message for r in caplog.records if r.message.startswith("routing.")]
    mention_lines = [l for l in routing_lines if l.startswith("routing.mention")]
    dropped_lines = [l for l in routing_lines if l.startswith("routing.dropped")]
    assert len(mention_lines) == 1, f"Expected 1 routing.mention, got: {mention_lines}"
    assert f"target_kind=issue" in mention_lines[0], mention_lines[0]
    assert f"target={issue_num}" in mention_lines[0], mention_lines[0]
    assert "action=inject_workflow_nova" in mention_lines[0], mention_lines[0]
    assert dropped_lines == [], f"Expected zero routing.dropped lines, got: {dropped_lines}"


@pytest.mark.asyncio
async def test_routing_ac3_dispatch_error_pr_unified_emitted_no_dropped(caplog):
    pr_num = 300
    impl = MagicMock()
    impl.mention = AsyncMock(side_effect=Exception("dispatch error"))
    monitor, github = _make_monitor_for_routing(implementer=impl)
    pr = PrRef(
        number=pr_num,
        title="PR test",
        url=f"https://github.com/o/r/pull/{pr_num}",
        labels=(),
        head_ref=f"auto/issue-{pr_num}",
    )
    github.get_pr = AsyncMock(return_value=pr)
    trigger = MentionTrigger(trigger_type="reviewer", pr=pr)
    with caplog.at_level(logging.INFO, logger="deile.pipeline.events"):
        await _dispatch_mention_group(
            monitor, f"pr:{pr_num}", [trigger], "deile-one", 0.0
        )
    routing_lines = [r.message for r in caplog.records if r.message.startswith("routing.")]
    pr_unified_lines = [l for l in routing_lines if l.startswith("routing.pr_unified")]
    dropped_lines = [l for l in routing_lines if l.startswith("routing.dropped")]
    assert len(pr_unified_lines) == 1, f"Expected 1 routing.pr_unified, got: {pr_unified_lines}"
    assert f"target={pr_num}" in pr_unified_lines[0], pr_unified_lines[0]
    assert "role=requested_reviewer" in pr_unified_lines[0], pr_unified_lines[0]
    assert dropped_lines == [], f"Expected zero routing.dropped lines, got: {dropped_lines}"
