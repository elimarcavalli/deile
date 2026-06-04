"""Assignee-in-issue loop is gated by ``~workflow:*`` labels (issue #483 V1 fix).

Issues that already carry a pipeline gate label (``~workflow:<anything>``) are
skipped by the assignee loop to prevent every tick re-arming a MentionTrigger
and flooding the EVENTS panel.

Issues WITHOUT any ``~workflow:*`` label (including those with only
``~mention:processado``) MUST still produce a trigger — this preserves the
Decisão #45 invariant tested in
``test_collector_assignee_not_gated_by_mention_done.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from deile.orchestration.pipeline.github_client import IssueRef
from deile.orchestration.pipeline.labels import MENTION_DONE
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.orchestration.pipeline.stages import _collect_mention_triggers


def _make_monitor(*, assigned_issues: list | None = None) -> PipelineMonitor:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        mention_handle="@deile-one",
    )
    github = MagicMock()
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.list_issues_assigned_to = AsyncMock(return_value=list(assigned_issues or []))
    github.list_prs_assigned_to = AsyncMock(return_value=[])
    github.list_prs_with_review_requests = AsyncMock(return_value=[])
    github.search_items_mentioning = AsyncMock(return_value=([], []))
    notifier = MagicMock()
    notifier.error = AsyncMock()
    return PipelineMonitor(
        cfg, github=github, worktrees=MagicMock(), claude=MagicMock(),
        notifier=notifier,
    )


def _issue(number: int, labels=()) -> IssueRef:
    return IssueRef(
        number=number, title="t", url=f"https://github.com/o/r/issues/{number}",
        labels=tuple(labels),
    )


class TestAssigneeIssueGatedByWorkflow:
    """The assignee-in-issue loop skips gate-owned issues (issue #483 V1 fix)."""

    async def test_issue_with_workflow_nova_is_skipped(self):
        """An issue with ``~workflow:nova`` must NOT produce a trigger."""
        monitor = _make_monitor(assigned_issues=[_issue(10, labels=("~workflow:nova",))])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert triggers == []

    async def test_issue_with_workflow_em_implementacao_is_skipped(self):
        """An issue deep in the pipeline must NOT produce a trigger."""
        monitor = _make_monitor(
            assigned_issues=[_issue(11, labels=("~workflow:em_implementacao",))]
        )
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert triggers == []

    async def test_issue_with_arbitrary_workflow_label_is_skipped(self):
        """Any ``~workflow:*`` variant must suppress the trigger."""
        for label in (
            "~workflow:em_revisao",
            "~workflow:revisada",
            "~workflow:decomposta",
            "~workflow:em_pr",
            "~workflow:bloqueada",
            "~workflow:em_refinamento",
            "~workflow:em_arquitetura",
            "~workflow:aguardando_stakeholder",
        ):
            monitor = _make_monitor(assigned_issues=[_issue(12, labels=(label,))])
            triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
            assert triggers == [], f"Expected no trigger for issue with label {label!r}"

    async def test_issue_without_workflow_label_produces_trigger(self):
        """An issue with no ``~workflow:*`` label MUST produce a trigger."""
        monitor = _make_monitor(assigned_issues=[_issue(20)])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].trigger_type == "assignee"
        assert triggers[0].issue is not None
        assert triggers[0].issue.number == 20

    async def test_issue_with_only_mention_done_produces_trigger(self):
        """``~mention:processado`` alone must NOT suppress the trigger.

        This preserves Decisão #45 / Decisão #32 invariant: the assignee loop
        is NOT gated by ``~mention:processado``.  The test mirrors
        ``test_assignee_issue_with_mention_done_still_arms`` in
        ``test_collector_assignee_not_gated_by_mention_done.py``.
        """
        monitor = _make_monitor(assigned_issues=[_issue(42, labels=(MENTION_DONE,))])
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        assert len(triggers) == 1
        assert triggers[0].issue is not None
        assert triggers[0].issue.number == 42

    async def test_mixed_batch_only_non_workflow_issues_fire(self):
        """When multiple issues are returned, only those without ``~workflow:*``
        produce triggers."""
        issues = [
            _issue(1, labels=("~workflow:nova",)),
            _issue(2),
            _issue(3, labels=("~workflow:em_implementacao",)),
            _issue(4, labels=(MENTION_DONE,)),
        ]
        monitor = _make_monitor(assigned_issues=issues)
        triggers = await _collect_mention_triggers(monitor, "@deile-one", "deile-one")
        trigger_numbers = {t.issue.number for t in triggers if t.issue}
        assert trigger_numbers == {2, 4}, (
            "Expected triggers for issues 2 and 4 (no ~workflow:* label); "
            f"got {trigger_numbers}"
        )
