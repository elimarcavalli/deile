"""Tests for issue #351: invalidate ~review:concluida on new commits.

Covers:
- ``_classify_new_commits`` classification logic
- ``_handle_review_concluded_invalidation`` invalidation flow
- ``review_one_open_pr`` pre-processing step
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.orchestration.pipeline.labels import (REVIEW_CONCLUDED,
                                                 REVIEW_PENDING)
from deile.orchestration.pipeline.stages import (
    CLASS_CODE, CLASS_COSMETIC, CLASS_DOCS_ONLY, _classify_new_commits,
    _handle_review_concluded_invalidation)

# ---------------------------------------------------------------------------
# _classify_new_commits
# ---------------------------------------------------------------------------


class TestClassifyNewCommits:
    """Unit tests for the commit classification heuristic."""

    def test_docs_only__all_files_in_docs_dir(self):
        commits = [{"sha": "a", "message": "m", "files": ["docs/api.md", "docs/guide.rst"]}]
        assert _classify_new_commits(commits) == CLASS_DOCS_ONLY

    def test_docs_only__all_files_md_extension(self):
        commits = [{"sha": "a", "message": "m", "files": ["README.md", "CHANGELOG.md"]}]
        assert _classify_new_commits(commits) == CLASS_DOCS_ONLY

    def test_docs_only__mixed_docs_prefix_and_ext(self):
        commits = [
            {"sha": "a", "message": "m", "files": ["docs/x.md"]},
            {"sha": "b", "message": "m", "files": ["README.rst"]},
        ]
        assert _classify_new_commits(commits) == CLASS_DOCS_ONLY

    def test_code__python_file_present(self):
        commits = [{"sha": "a", "message": "fix", "files": ["deile/stages.py"]}]
        assert _classify_new_commits(commits) == CLASS_CODE

    def test_code__mixed_docs_and_code(self):
        commits = [
            {"sha": "a", "message": "fix", "files": ["docs/x.md", "deile/stages.py"]},
        ]
        assert _classify_new_commits(commits) == CLASS_CODE

    def test_code__typescript_file(self):
        commits = [{"sha": "a", "message": "fix", "files": ["src/app.ts"]}]
        assert _classify_new_commits(commits) == CLASS_CODE

    def test_code__yaml_file(self):
        commits = [{"sha": "a", "message": "ci", "files": [".github/workflows/ci.yaml"]}]
        assert _classify_new_commits(commits) == CLASS_CODE

    def test_cosmetic__config_only(self):
        commits = [{"sha": "a", "message": "chore", "files": [".gitignore", "Makefile"]}]
        assert _classify_new_commits(commits) == CLASS_COSMETIC

    def test_empty_commits__safe_default_code(self):
        assert _classify_new_commits([]) == CLASS_CODE

    def test_no_files_info__safe_default_code(self):
        commits = [{"sha": "a", "message": "m", "files": []}]
        assert _classify_new_commits(commits) == CLASS_CODE

    def test_multiple_commits_all_docs(self):
        commits = [
            {"sha": "a", "message": "", "files": ["docs/a.md"]},
            {"sha": "b", "message": "", "files": ["docs/b.rst"]},
            {"sha": "c", "message": "", "files": ["CHANGELOG.adoc"]},
        ]
        assert _classify_new_commits(commits) == CLASS_DOCS_ONLY

    def test_code_file_js_detected(self):
        commits = [{"sha": "a", "message": "", "files": ["main.js"]}]
        assert _classify_new_commits(commits) == CLASS_CODE

    def test_code_file_go_detected(self):
        commits = [{"sha": "a", "message": "", "files": ["main.go"]}]
        assert _classify_new_commits(commits) == CLASS_CODE

    def test_multiple_non_code_non_docs__cosmetic(self):
        commits = [{"sha": "a", "message": "", "files": ["Dockerfile", ".env.example"]}]
        assert _classify_new_commits(commits) == CLASS_COSMETIC


# ---------------------------------------------------------------------------
# _handle_review_concluded_invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidation_no_new_commits__keeps_concluded():
    """No new commits → nothing changes, no labels removed."""
    forge = MagicMock()
    forge.label_applied_at = AsyncMock(return_value=1700000000)
    forge.get_pr_commits_since = AsyncMock(return_value=[])  # no commits
    forge.remove_labels = AsyncMock()
    forge.add_labels = AsyncMock()
    forge.comment_on_pr = AsyncMock()

    monitor = MagicMock()
    monitor.forge = forge

    pr = MagicMock()
    pr.number = 100

    await _handle_review_concluded_invalidation(monitor, pr)

    # Labels untouched.
    forge.remove_labels.assert_not_called()
    forge.add_labels.assert_not_called()
    forge.comment_on_pr.assert_not_called()


@pytest.mark.asyncio
async def test_invalidation_cosmetic__comments_keeps_label():
    """Cosmetic commits → comment only, label stays."""
    forge = MagicMock()
    forge.label_applied_at = AsyncMock(return_value=1700000000)
    forge.get_pr_commits_since = AsyncMock(return_value=[
        {"sha": "abc", "message": "chore", "files": [".gitignore"]},
    ])
    forge.remove_labels = AsyncMock()
    forge.add_labels = AsyncMock()
    forge.comment_on_pr = AsyncMock()

    monitor = MagicMock()
    monitor.forge = forge

    pr = MagicMock()
    pr.number = 200

    await _handle_review_concluded_invalidation(monitor, pr)

    # Comment posted.
    forge.comment_on_pr.assert_awaited_once()
    comment_text = forge.comment_on_pr.call_args[0][1]
    assert "cosmético" in comment_text.lower() or "cosmético" in comment_text

    # Labels NOT removed/added.
    forge.remove_labels.assert_not_called()
    forge.add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_invalidation_code__removes_concluded_adds_pending():
    """Code commits → remove ~review:concluida, add ~review:pendente, comment."""
    forge = MagicMock()
    forge.label_applied_at = AsyncMock(return_value=1700000000)
    forge.get_pr_commits_since = AsyncMock(return_value=[
        {"sha": "abc", "message": "fix bug", "files": ["deile/stages.py"]},
    ])
    forge.remove_labels = AsyncMock()
    forge.add_labels = AsyncMock()
    forge.comment_on_pr = AsyncMock()

    monitor = MagicMock()
    monitor.forge = forge

    pr = MagicMock()
    pr.number = 300

    await _handle_review_concluded_invalidation(monitor, pr)

    # Remove REVIEW_CONCLUDED.
    forge.remove_labels.assert_awaited_once()
    remove_args = forge.remove_labels.call_args
    assert remove_args[0][0] == "pr"
    assert remove_args[0][1] == 300
    assert REVIEW_CONCLUDED in remove_args[0][2]

    # Add REVIEW_PENDING.
    forge.add_labels.assert_awaited_once()
    add_args = forge.add_labels.call_args
    assert add_args[0][0] == "pr"
    assert add_args[0][1] == 300
    assert REVIEW_PENDING in add_args[0][2]

    # Comment posted.
    forge.comment_on_pr.assert_awaited_once()
    comment_text = forge.comment_on_pr.call_args[0][1]
    assert "código" in comment_text or "código" in comment_text


@pytest.mark.asyncio
async def test_invalidation_docs_only__removes_concluded_adds_pending():
    """Docs-only commits → remove ~review:concluida, add ~review:pendente."""
    forge = MagicMock()
    forge.label_applied_at = AsyncMock(return_value=1700000000)
    forge.get_pr_commits_since = AsyncMock(return_value=[
        {"sha": "abc", "message": "update docs", "files": ["docs/api.md"]},
    ])
    forge.remove_labels = AsyncMock()
    forge.add_labels = AsyncMock()
    forge.comment_on_pr = AsyncMock()

    monitor = MagicMock()
    monitor.forge = forge

    pr = MagicMock()
    pr.number = 400

    await _handle_review_concluded_invalidation(monitor, pr)

    forge.remove_labels.assert_awaited_once()
    forge.add_labels.assert_awaited_once()
    forge.comment_on_pr.assert_awaited_once()

    comment_text = forge.comment_on_pr.call_args[0][1]
    assert "docs-only" in comment_text


@pytest.mark.asyncio
async def test_invalidation_no_label_timestamp__skips():
    """label_applied_at returns None → skip, nothing touched."""
    forge = MagicMock()
    forge.label_applied_at = AsyncMock(return_value=None)
    forge.get_pr_commits_since = AsyncMock()
    forge.remove_labels = AsyncMock()
    forge.add_labels = AsyncMock()
    forge.comment_on_pr = AsyncMock()

    monitor = MagicMock()
    monitor.forge = forge

    pr = MagicMock()
    pr.number = 500

    await _handle_review_concluded_invalidation(monitor, pr)

    forge.get_pr_commits_since.assert_not_called()
    forge.remove_labels.assert_not_called()
    forge.add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_invalidation_commits_fetch_error__skips():
    """get_pr_commits_since raises → skip, keep label."""
    forge = MagicMock()
    forge.label_applied_at = AsyncMock(return_value=1700000000)
    forge.get_pr_commits_since = AsyncMock(side_effect=RuntimeError("net error"))
    forge.remove_labels = AsyncMock()
    forge.add_labels = AsyncMock()

    monitor = MagicMock()
    monitor.forge = forge

    pr = MagicMock()
    pr.number = 600

    # Should not raise — best-effort.
    await _handle_review_concluded_invalidation(monitor, pr)

    forge.remove_labels.assert_not_called()
    forge.add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_invalidation_remove_label_fails__skips():
    """remove_labels fails after classification → returns early, no label changes."""
    forge = MagicMock()
    forge.label_applied_at = AsyncMock(return_value=1700000000)
    forge.get_pr_commits_since = AsyncMock(return_value=[
        {"sha": "abc", "message": "fix", "files": ["deile/stages.py"]},
    ])
    from deile.orchestration.forge.github_forge import GhCommandError
    forge.remove_labels = AsyncMock(side_effect=GhCommandError(
        ("gh", "api"), 1, "", "GH API down",
    ))
    forge.add_labels = AsyncMock()
    forge.comment_on_pr = AsyncMock()

    monitor = MagicMock()
    monitor.forge = forge

    pr = MagicMock()
    pr.number = 700

    await _handle_review_concluded_invalidation(monitor, pr)

    # Tried to remove.
    forge.remove_labels.assert_called()
    # But add_labels was never called (we returned early).
    forge.add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_invalidation_add_pending_fails__re_adds_concluded():
    """remove_labels succeeds but add_labels(REVIEW_PENDING) fails → recovery
    re-adds REVIEW_CONCLUDED so the PR isn't left label-less."""
    forge = MagicMock()
    forge.label_applied_at = AsyncMock(return_value=1700000000)
    forge.get_pr_commits_since = AsyncMock(return_value=[
        {"sha": "abc", "message": "fix", "files": ["deile/stages.py"]},
    ])
    forge.remove_labels = AsyncMock()  # succeeds
    from deile.orchestration.forge.github_forge import GhCommandError

    # First call (add REVIEW_PENDING) fails; second call (re-add CONCLUDED) succeeds.
    forge.add_labels = AsyncMock(side_effect=[
        GhCommandError(("gh", "api"), 1, "", "GH API down"),
        None,  # re-add succeeds
    ])
    forge.comment_on_pr = AsyncMock()

    monitor = MagicMock()
    monitor.forge = forge

    pr = MagicMock()
    pr.number = 701

    await _handle_review_concluded_invalidation(monitor, pr)

    # Tried to remove.
    forge.remove_labels.assert_called_once()
    # Tried to add twice: first REVIEW_PENDING (failed), then REVIEW_CONCLUDED.
    assert forge.add_labels.call_count == 2
    first_add_labels = forge.add_labels.call_args_list[0][0][2]
    second_add_labels = forge.add_labels.call_args_list[1][0][2]
    assert REVIEW_PENDING in first_add_labels
    assert REVIEW_CONCLUDED in second_add_labels


# ---------------------------------------------------------------------------
# review_one_open_pr pre-processing integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_one_open_pr_skips_concluded_pr_without_new_commits():
    """PR with ~review:concluida and NO new commits → still excluded."""
    from pathlib import Path

    from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                      PipelineMonitor)

    cfg = PipelineConfig(
        repo="owner/r", base_repo_path=Path("/tmp"), notify_user_id="42",
        use_pid_lock=False, reaper_stale_seconds=0,
    )
    forge = MagicMock()
    forge.list_open_prs = AsyncMock(return_value=[])
    forge.list_issues_with_label = AsyncMock(return_value=[])
    # concluded PR exists but has no new commits
    concluded_pr = MagicMock()
    concluded_pr.number = 999
    concluded_pr.head_ref = "auto/issue-999"
    concluded_pr.is_draft = False
    concluded_pr.labels = [REVIEW_CONCLUDED]
    concluded_pr.batch_id = None

    forge.list_open_prs = AsyncMock(return_value=[concluded_pr])
    forge.label_applied_at = AsyncMock(return_value=1700000000)
    forge.get_pr_commits_since = AsyncMock(return_value=[])  # no new commits
    forge.claim_with_batch = AsyncMock()
    forge.transition_pr = AsyncMock()

    notifier = MagicMock()
    notifier.pr_picked_up = AsyncMock()
    notifier.pr_reviewed = AsyncMock()

    monitor = PipelineMonitor(
        cfg, github=forge, worktrees=MagicMock(), claude=MagicMock(),
        notifier=notifier,
    )
    monitor.implementer = MagicMock()

    from deile.orchestration.pipeline.stages import review_one_open_pr
    await review_one_open_pr(monitor)

    # PR was NOT picked up for review.
    forge.claim_with_batch.assert_not_called()


@pytest.mark.asyncio
async def test_review_one_open_pr_invalidates_concluded_with_new_code_commits():
    """PR with ~review:concluida and new CODE commits → invalidated."""
    from pathlib import Path

    from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                      PipelineMonitor)

    cfg = PipelineConfig(
        repo="owner/r", base_repo_path=Path("/tmp"), notify_user_id="42",
        use_pid_lock=False, reaper_stale_seconds=0,
    )
    forge = MagicMock()
    forge.list_issues_with_label = AsyncMock(return_value=[])

    concluded_pr = MagicMock()
    concluded_pr.number = 888
    concluded_pr.head_ref = "auto/issue-888"
    concluded_pr.is_draft = False
    concluded_pr.labels = [REVIEW_CONCLUDED]
    concluded_pr.batch_id = None

    # Also include a fresh PR so the stage has a candidate after invalidation.
    fresh_pr = MagicMock()
    fresh_pr.number = 999
    fresh_pr.head_ref = "auto/issue-999"
    fresh_pr.is_draft = False
    fresh_pr.labels = [REVIEW_PENDING]
    fresh_pr.batch_id = None

    forge.list_open_prs = AsyncMock(return_value=[concluded_pr, fresh_pr])
    forge.label_applied_at = AsyncMock(return_value=1700000000)
    forge.get_pr_commits_since = AsyncMock(return_value=[
        {"sha": "abc", "message": "fix", "files": ["deile/stages.py"]},
    ])
    forge.remove_labels = AsyncMock()
    forge.add_labels = AsyncMock()
    forge.comment_on_pr = AsyncMock()
    forge.claim_with_batch = AsyncMock(return_value="batch1")
    forge.transition_pr = AsyncMock()
    forge.clear_batch_label = AsyncMock()
    forge.has_bot_activity_since = AsyncMock(return_value=True)

    notifier = MagicMock()
    notifier.pr_picked_up = AsyncMock()
    notifier.pr_reviewed = AsyncMock()

    monitor = PipelineMonitor(
        cfg, github=forge, worktrees=MagicMock(), claude=MagicMock(),
        notifier=notifier,
    )
    from deile.orchestration.pipeline.implementer import WorkOutcome
    monitor.implementer = MagicMock()
    # Review succeeds with merge.
    monitor.implementer.review = AsyncMock(return_value=WorkOutcome(
        ok=True, text="merged successfully",
    ))

    from deile.orchestration.pipeline.stages import review_one_open_pr
    await review_one_open_pr(monitor)

    # The concluded PR had its label removed and pending added.
    remove_calls = [
        c for c in forge.remove_labels.call_args_list
        if REVIEW_CONCLUDED in c[0][2]
    ]
    assert len(remove_calls) >= 1, "Expected REVIEW_CONCLUDED to be removed"

    add_pending_calls = [
        c for c in forge.add_labels.call_args_list
        if REVIEW_PENDING in c[0][2]
    ]
    assert len(add_pending_calls) >= 1, "Expected REVIEW_PENDING to be added"

    # Comment was posted on the concluded PR.
    forge.comment_on_pr.assert_awaited()
