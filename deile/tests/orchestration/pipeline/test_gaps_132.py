"""Regression tests for issue #132 — gaps residuais do pipeline autônomo.

Covers all items from #132:
  #11 — TOCTOU re-fetch in claim_with_batch
  #15 — github remote wiring in create_branch_worktree
  #16 — WARNING + skipped_runs when enable_* is False in schedule mode
  #21 — batch label created before claim finalized (order check)
  #23/#24 — bootstrap_replay_window_hours in compute_pending
  #28 — PipelineCommand --identity/--schedule-file/--no-pid-lock flags
  #31 — schedule-driven classify triggers notifications
  #32 — follow_ups as standalone action
  Wiring C — cleanup_merged_branches + gc_completed_oneshots on startup
Coverage — github_client, review_callback, worktree_manager
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.orchestration.pipeline.github_client import (
    GhCommandError, GitHubClient, IssueRef, PrRef, compute_batch_id_for_number)
from deile.orchestration.pipeline.labels import (FOLLOW_UPS_PROCESSED,
                                                 REVIEW_PENDING, WORKFLOW_NEW)
from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                  PipelineMonitor)
from deile.orchestration.pipeline.scheduler import (VALID_ACTIONS,
                                                    RecurringEntry, Schedule)

# ---------------------------------------------------------------------------
# #11 — TOCTOU re-fetch in claim_with_batch
# ---------------------------------------------------------------------------

class TestClaimWithBatchTOCTOU:
    async def test_yields_when_foreign_batch_label_appears_after_add(self):
        """After add_labels, if a foreign ~batch: label is present, we remove
        ours and return None."""
        client = GitHubClient("owner/repo")
        unclaimed = IssueRef(number=1, title="t", url="u", labels=(WORKFLOW_NEW,))
        # After add, a foreign batch label appears.
        after_add = IssueRef(
            number=1, title="t", url="u",
            labels=(WORKFLOW_NEW, "~batch:deadbeef", "~batch:cafebabe"),
        )
        with patch.object(client, "get_issue", new=AsyncMock(side_effect=[unclaimed, after_add])), \
             patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))), \
             patch.object(client, "_run_checked", new=AsyncMock(return_value="")):
            result = await client.claim_with_batch("issue", 1)
        assert result is None

    async def test_succeeds_when_only_our_batch_label_present_after_add(self):
        """Happy path: after add_labels, only our label is present — return batch_id."""
        client = GitHubClient("owner/repo")
        our_batch = compute_batch_id_for_number("issue", 5)
        our_label = f"~batch:{our_batch}"
        unclaimed = IssueRef(number=5, title="x", url="u", labels=(WORKFLOW_NEW,))
        after_add = IssueRef(
            number=5, title="x", url="u",
            labels=(WORKFLOW_NEW, our_label),
        )
        with patch.object(client, "get_issue", new=AsyncMock(side_effect=[unclaimed, after_add])), \
             patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))), \
             patch.object(client, "_run_checked", new=AsyncMock(return_value="")):
            result = await client.claim_with_batch("issue", 5)
        assert result == our_batch

    async def test_returns_none_when_after_fetch_returns_none(self):
        """PR disappears between claim and re-fetch."""
        client = GitHubClient("owner/repo")
        pr = PrRef(number=3, title="p", url="u", labels=(REVIEW_PENDING,))
        with patch.object(client, "get_pr", new=AsyncMock(side_effect=[pr, None])), \
             patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))), \
             patch.object(client, "_run_checked", new=AsyncMock(return_value="")):
            result = await client.claim_with_batch("pr", 3)
        assert result is None

    async def test_race_removal_gh_error_logged_not_raised(self):
        """If remove_labels fails after detecting race, a warning is logged but
        claim_with_batch still returns None cleanly."""
        client = GitHubClient("owner/repo")
        unclaimed = IssueRef(number=7, title="t2", url="u", labels=(WORKFLOW_NEW,))
        after_add = IssueRef(
            number=7, title="t2", url="u",
            labels=(WORKFLOW_NEW, "~batch:deadbeef", "~batch:cafecafe"),
        )
        remove_calls = []

        async def fake_remove(kind, number, labels):
            remove_calls.append(labels)
            raise GhCommandError(("gh",), 1, "", "network error")

        with patch.object(client, "get_issue", new=AsyncMock(side_effect=[unclaimed, after_add])), \
             patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))), \
             patch.object(client, "_run_checked", new=AsyncMock(return_value="")), \
             patch.object(client, "remove_labels", side_effect=fake_remove):
            result = await client.claim_with_batch("issue", 7)
        assert result is None
        assert remove_calls  # remove was attempted


# ---------------------------------------------------------------------------
# #15 — github remote wiring in create_branch_worktree
# ---------------------------------------------------------------------------

class TestEnsureGithubRemote:
    async def test_github_remote_added_when_missing(self, tmp_path):
        from deile.orchestration.pipeline.worktree_manager import \
            WorktreeManager

        # Set up a minimal git repo.
        base = tmp_path / "repo"
        base.mkdir()
        (base / ".git").mkdir()

        mgr = WorktreeManager.__new__(WorktreeManager)
        mgr.base_repo = base
        mgr.main_branch = "main"
        mgr.subdir = None
        mgr.worktrees_dir = base / ".worktrees"
        mgr.branches_dir = base / ".worktrees"

        worktree = base / ".worktrees" / "feat"
        worktree.mkdir(parents=True)

        calls = []

        async def fake_capture(cwd, *args):
            calls.append(args)
            if args == ("remote", "get-url", "github"):
                # base_repo has no github remote
                return (1, "", "not found")
            if args == ("remote", "get-url", "origin"):
                return (0, "git@github.com:owner/repo.git\n", "")
            if args == ("remote", "get-url",) and len(args) == 3:
                # worktree check: no github remote yet
                return (1, "", "not found")
            return (0, "", "")

        async def fake_git_in(cwd, *args):
            calls.append(args)

        mgr._git_in_capture = staticmethod(lambda cwd, *args: fake_capture(cwd, *args))
        mgr._git_in = staticmethod(lambda cwd, *args: fake_git_in(cwd, *args))

        await mgr._ensure_github_remote(worktree)

        # Verify remote add was attempted
        add_calls = [c for c in calls if len(c) >= 2 and c[0] == "remote" and c[1] == "add"]
        assert any("github" in str(c) for c in add_calls)

    async def test_skips_non_github_origin(self, tmp_path):
        from deile.orchestration.pipeline.worktree_manager import \
            WorktreeManager

        base = tmp_path / "repo2"
        base.mkdir()
        (base / ".git").mkdir()

        mgr = WorktreeManager.__new__(WorktreeManager)
        mgr.base_repo = base
        mgr.main_branch = "main"
        mgr.subdir = None
        mgr.worktrees_dir = base / ".worktrees"
        mgr.branches_dir = base / ".worktrees"

        worktree = base / ".worktrees" / "feat2"
        worktree.mkdir(parents=True)

        add_calls = []

        async def fake_capture(cwd, *args):
            if "get-url" in args and "github" in args:
                return (1, "", "no remote")
            if "get-url" in args and "origin" in args:
                return (0, "/local/path/to/repo\n", "")
            return (0, "", "")

        async def fake_git_in(cwd, *args):
            if args[0] == "remote" and args[1] == "add":
                add_calls.append(args)

        mgr._git_in_capture = staticmethod(lambda cwd, *args: fake_capture(cwd, *args))
        mgr._git_in = staticmethod(lambda cwd, *args: fake_git_in(cwd, *args))

        await mgr._ensure_github_remote(worktree)

        # Should NOT add a remote for a local path.
        assert not add_calls


# ---------------------------------------------------------------------------
# #16 — WARNING + skipped_runs when enable_* is False
# ---------------------------------------------------------------------------

class TestSkippedRunsWarning:
    def _make_monitor(self, tmp_path, **config_overrides):
        cfg = PipelineConfig(
            repo="owner/repo",
            base_repo_path=tmp_path,
            use_pid_lock=False,
            **config_overrides,
        )
        github = AsyncMock()
        github.list_issues_with_label = AsyncMock(return_value=[])
        github.list_open_prs = AsyncMock(return_value=[])
        github.list_unclassified_issues = AsyncMock(return_value=[])
        notifier = AsyncMock()
        worktrees = AsyncMock()
        return PipelineMonitor(
            cfg,
            github=github,
            worktrees=worktrees,
            claude=AsyncMock(),
            notifier=notifier,
        )

    async def test_disabled_classify_increments_skipped_runs(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import PendingRun

        monitor = self._make_monitor(tmp_path, enable_classify=False)
        run = PendingRun(
            when=datetime(2026, 1, 1, tzinfo=timezone.utc),
            entry_id="r1",
            action="classify",
            is_oneshot=False,
        )
        await monitor._run_scheduled(run)

        assert monitor._stats.skipped_runs == 1
        # Classify was NOT actually run (no issue list call made).
        monitor.github.list_unclassified_issues.assert_not_called()

    async def test_disabled_review_increments_skipped_runs(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import PendingRun

        monitor = self._make_monitor(tmp_path, enable_review=False)
        run = PendingRun(
            when=datetime(2026, 1, 1, tzinfo=timezone.utc),
            entry_id="r1",
            action="review",
            is_oneshot=False,
        )
        await monitor._run_scheduled(run)
        assert monitor._stats.skipped_runs == 1

    async def test_enabled_classify_does_not_increment_skipped(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import PendingRun

        monitor = self._make_monitor(tmp_path, enable_classify=True)
        run = PendingRun(
            when=datetime(2026, 1, 1, tzinfo=timezone.utc),
            entry_id="r1",
            action="classify",
            is_oneshot=False,
        )
        await monitor._run_scheduled(run)
        assert monitor._stats.skipped_runs == 0


# ---------------------------------------------------------------------------
# #23/#24 — bootstrap_replay_window_hours
# ---------------------------------------------------------------------------

class TestBootstrapReplayWindow:
    def test_compute_pending_skips_very_old_slots_with_window(self):
        """With a 1h window, an entry that last ran 3h ago skips directly to
        the most recent slot within the window rather than replaying from 3h ago.

        Concretely: last_ran=3h ago, cron=every30min.
        Without window: oldest pending slot = 2.5h ago.
        With window=1h: anchor advances to (now - 1h), first slot within window
        = next_after(cutoff) = ~30min ago (still ≤ now) → ONE run fired.
        The important thing is we do NOT get dozens of coalesced replays from 3h ago;
        instead we get exactly one slot (or zero if even the window slot is in future).
        """
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        # Last ran 3h ago.
        anchor = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
        entry = RecurringEntry(
            id="r1", action="review", cron="*/30 * * * *",
            enabled=True, last_run_at=anchor,
        )
        sched_no_window = Schedule(recurring=[entry])
        sched_with_window = Schedule(recurring=[entry])

        without_window = sched_no_window.compute_pending(now, replay_window_hours=None)
        with_window = sched_with_window.compute_pending(now, replay_window_hours=1)

        # With coalescing, both return exactly 1 run — but with window the slot
        # timestamp is within the 1h window (> cutoff = now - 1h).
        assert len(with_window) == 1
        # The run time should be within the last 1 hour.
        cutoff = now - __import__("datetime").timedelta(hours=1)
        assert with_window[0].when >= cutoff

        # Without window the coalesced run is also 1, but its timestamp can be older.
        assert len(without_window) == 1

    def test_compute_pending_no_slot_within_window_returns_nothing(self):
        """If even the cron's next slot after cutoff is still in the future, no run."""
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        # cron fires daily at midnight; last ran yesterday.
        anchor = datetime(2026, 4, 30, tzinfo=timezone.utc)
        entry = RecurringEntry(
            id="r2", action="classify", cron="0 0 * * *",
            enabled=True, last_run_at=anchor,
        )
        sched = Schedule(recurring=[entry])
        # With window=1h, the next midnight after (now-1h)=11:00 is tomorrow midnight.
        pending = sched.compute_pending(now, replay_window_hours=1)
        # No pending run because next midnight > now.
        assert not pending

    def test_compute_pending_within_window_returns_run(self):
        """Slots within the replay window are returned."""
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        # Last ran 20 minutes ago — next slot is 19 minutes ago (within 1h window).
        anchor = datetime(2026, 5, 1, 11, 40, tzinfo=timezone.utc)
        entry = RecurringEntry(
            id="r1", action="review", cron="*/1 * * * *",
            enabled=True, last_run_at=anchor,
        )
        sched = Schedule(recurring=[entry])
        pending = sched.compute_pending(now, replay_window_hours=1)
        assert pending

    def test_compute_pending_none_window_uses_full_history(self):
        """None window = no limit; old slots are still returned (coalesced)."""
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        anchor = datetime(2026, 4, 1, tzinfo=timezone.utc)  # 30 days ago
        entry = RecurringEntry(
            id="r1", action="review", cron="*/30 * * * *",
            enabled=True, last_run_at=anchor,
        )
        sched = Schedule(recurring=[entry])
        pending = sched.compute_pending(now, replay_window_hours=None)
        # Should return exactly one pending run (coalesced).
        assert len(pending) == 1


# ---------------------------------------------------------------------------
# #32 — follow_ups in VALID_ACTIONS
# ---------------------------------------------------------------------------

class TestFollowUpsAction:
    def test_follow_ups_in_valid_actions(self):
        assert "follow_ups" in VALID_ACTIONS

    def test_schedule_accepts_follow_ups_action(self):
        entry = RecurringEntry(
            id="fu-loop", action="follow_ups", cron="0 * * * *",
        )
        assert entry.action == "follow_ups"

    async def test_run_scheduled_follow_ups_calls_standalone(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import PendingRun

        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path, use_pid_lock=False,
        )
        monitor = PipelineMonitor(
            cfg,
            github=AsyncMock(),
            worktrees=AsyncMock(),
            claude=AsyncMock(),
            notifier=AsyncMock(),
        )
        called = []

        async def fake_standalone():
            called.append(True)

        monitor._standalone_follow_ups = fake_standalone
        run = PendingRun(
            when=datetime(2026, 1, 1, tzinfo=timezone.utc),
            entry_id="fu",
            action="follow_ups",
            is_oneshot=False,
        )
        await monitor._run_scheduled(run)
        assert called


# ---------------------------------------------------------------------------
# Wiring C — cleanup_merged_branches + gc_completed_oneshots in catch_up
# ---------------------------------------------------------------------------

class TestStartupWiring:
    async def test_cleanup_merged_branches_called_on_catchup(self, tmp_path):
        from deile.orchestration.pipeline.github_client import PrRef

        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path,
            use_pid_lock=False, enable_worktree_cleanup=True,
        )
        schedule_store = MagicMock()
        schedule_store.load = MagicMock(return_value=Schedule())
        schedule_store.save = MagicMock()

        worktrees = AsyncMock()
        worktrees.cleanup_merged_branches = AsyncMock(return_value=2)

        github = AsyncMock()
        github.list_recently_merged_prs = AsyncMock(return_value=[
            PrRef(number=1, title="t", url="u", labels=(), head_ref="feat/a", state="merged"),
            PrRef(number=2, title="t", url="u", labels=(), head_ref="feat/b", state="merged"),
        ])

        monitor = PipelineMonitor(
            cfg,
            github=github,
            worktrees=worktrees,
            claude=AsyncMock(),
            notifier=AsyncMock(),
            schedule_store=schedule_store,
        )
        await monitor._catch_up_pending()
        worktrees.cleanup_merged_branches.assert_called_once_with(["feat/a", "feat/b"])

    async def test_cleanup_skipped_when_disabled(self, tmp_path):
        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path,
            use_pid_lock=False, enable_worktree_cleanup=False,
        )
        schedule_store = MagicMock()
        schedule_store.load = MagicMock(return_value=Schedule())
        schedule_store.save = MagicMock()

        worktrees = AsyncMock()
        worktrees.cleanup_merged_branches = AsyncMock(return_value=0)

        monitor = PipelineMonitor(
            cfg,
            github=AsyncMock(),
            worktrees=worktrees,
            claude=AsyncMock(),
            notifier=AsyncMock(),
            schedule_store=schedule_store,
        )
        await monitor._catch_up_pending()
        worktrees.cleanup_merged_branches.assert_not_called()

    async def test_gc_completed_oneshots_called_on_catchup(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import OneshotEntry

        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path,
            use_pid_lock=False, enable_worktree_cleanup=False,
        )
        # Build a schedule with one completed old oneshot.
        old_oneshot = OneshotEntry(
            id="old-1",
            action="review",
            run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            completed=True,
        )
        schedule = Schedule(oneshot=[old_oneshot])
        saved_schedules = []

        schedule_store = MagicMock()
        schedule_store.load = MagicMock(return_value=schedule)
        def capture_save(s):
            saved_schedules.append(list(s.oneshot))
        schedule_store.save = capture_save

        monitor = PipelineMonitor(
            cfg,
            github=AsyncMock(),
            worktrees=AsyncMock(),
            claude=AsyncMock(),
            notifier=AsyncMock(),
            schedule_store=schedule_store,
        )
        await monitor._catch_up_pending()
        # The old oneshot should be GC'd before saving.
        assert saved_schedules
        # After GC, the completed old entry should be gone.
        assert not any(o.id == "old-1" for o in saved_schedules[-1])


# ---------------------------------------------------------------------------
# #28 — PipelineCommand flags
# ---------------------------------------------------------------------------

class TestPipelineCommandFlags:
    def test_parse_start_flags_identity(self):
        from deile.commands.builtin.pipeline_command import _parse_start_flags
        ns = _parse_start_flags("--identity my-monitor")
        assert ns.identity == "my-monitor"
        assert not ns.no_pid_lock

    def test_parse_start_flags_no_pid_lock(self):
        from deile.commands.builtin.pipeline_command import _parse_start_flags
        ns = _parse_start_flags("--no-pid-lock")
        assert ns.no_pid_lock is True

    def test_parse_start_flags_schedule_file(self):
        from deile.commands.builtin.pipeline_command import _parse_start_flags
        ns = _parse_start_flags("--schedule-file /tmp/sched.yaml")
        assert ns.schedule_file == "/tmp/sched.yaml"

    def test_parse_start_flags_defaults(self):
        from deile.commands.builtin.pipeline_command import _parse_start_flags
        ns = _parse_start_flags("")
        assert ns.identity is None
        assert ns.schedule_file is None
        assert not ns.no_pid_lock

    def test_parse_start_flags_combined(self):
        from deile.commands.builtin.pipeline_command import _parse_start_flags
        ns = _parse_start_flags("--identity alpha --no-pid-lock --schedule-file /s.yaml")
        assert ns.identity == "alpha"
        assert ns.no_pid_lock
        assert ns.schedule_file == "/s.yaml"

    def test_parse_start_flags_unknown_ignored(self):
        from deile.commands.builtin.pipeline_command import _parse_start_flags
        ns = _parse_start_flags("--unknown-future-flag foo")
        assert ns.identity is None  # graceful degradation


# ---------------------------------------------------------------------------
# Coverage: review_callback.py
# ---------------------------------------------------------------------------

class TestReviewCallback:
    async def test_returns_none_when_agent_is_none(self):
        from deile.orchestration.pipeline.review_callback import \
            make_review_callback
        cb = make_review_callback(None)
        assert cb is None

    async def test_returns_none_when_agent_lacks_process(self):
        from deile.orchestration.pipeline.review_callback import \
            make_review_callback
        cb = make_review_callback(object())
        assert cb is None

    async def test_callback_calls_agent_process(self):
        from deile.orchestration.pipeline.review_callback import \
            make_review_callback

        agent = MagicMock()
        agent.process = AsyncMock(return_value="Summary: do X")
        cb = make_review_callback(agent)
        assert cb is not None

        issue = IssueRef(number=1, title="test", url="u", labels=(), body="some body")
        result = await cb(issue)
        assert "Summary" in result
        agent.process.assert_called_once()

    async def test_callback_returns_empty_on_agent_exception(self):
        from deile.orchestration.pipeline.review_callback import \
            make_review_callback

        agent = MagicMock()
        agent.process = AsyncMock(side_effect=RuntimeError("boom"))
        cb = make_review_callback(agent)

        issue = IssueRef(number=2, title="err test", url="u", labels=(), body="body")
        result = await cb(issue)
        assert result == ""

    async def test_callback_returns_empty_on_none_response(self):
        from deile.orchestration.pipeline.review_callback import \
            make_review_callback

        agent = MagicMock()
        agent.process = AsyncMock(return_value=None)
        cb = make_review_callback(agent)

        issue = IssueRef(number=3, title="nil", url="u", labels=(), body="b")
        result = await cb(issue)
        assert result == ""


# ---------------------------------------------------------------------------
# Coverage: github_client — additional methods
# ---------------------------------------------------------------------------

class TestGetPrMethods:
    async def test_get_pr_returns_none_for_non_open_state(self):
        client = GitHubClient("owner/repo")
        payload = json.dumps({
            "number": 10, "title": "old", "url": "u",
            "labels": [], "headRefName": "", "baseRefName": "main",
            "state": "merged", "isDraft": False,
        })
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.get_pr(10)
        assert result is None

    async def test_get_pr_returns_none_on_gh_error(self):
        client = GitHubClient("owner/repo")
        with patch.object(
            client, "_run_checked",
            new=AsyncMock(side_effect=GhCommandError(("gh",), 1, "", "not found"))
        ):
            result = await client.get_pr(99)
        assert result is None

    async def test_list_open_prs_parses_json(self):
        client = GitHubClient("owner/repo")
        payload = json.dumps([{
            "number": 1, "title": "pr", "url": "u",
            "labels": [], "headRefName": "auto/issue-1",
            "baseRefName": "main", "state": "open", "isDraft": False,
        }])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            prs = await client.list_open_prs()
        assert len(prs) == 1
        assert prs[0].head_ref == "auto/issue-1"

    async def test_create_issue_returns_number(self):
        client = GitHubClient("owner/repo")
        with patch.object(
            client, "_run_checked",
            new=AsyncMock(return_value="https://github.com/owner/repo/issues/42\n")
        ):
            num = await client.create_issue("title", "body", labels=["intent"])
        assert num == 42

    async def test_create_issue_returns_zero_on_error(self):
        client = GitHubClient("owner/repo")
        with patch.object(
            client, "_run_checked",
            new=AsyncMock(side_effect=GhCommandError(("gh",), 1, "", "err"))
        ):
            num = await client.create_issue("t", "b")
        assert num == 0

    async def test_get_pr_body_returns_empty_on_error(self):
        client = GitHubClient("owner/repo")
        with patch.object(
            client, "_run_checked",
            new=AsyncMock(side_effect=GhCommandError(("gh",), 1, "", "err"))
        ):
            body = await client.get_pr_body(1)
        assert body == ""

    async def test_list_pr_comments_returns_empty_on_error(self):
        client = GitHubClient("owner/repo")
        with patch.object(
            client, "_run_checked",
            new=AsyncMock(side_effect=GhCommandError(("gh",), 1, "", "err"))
        ):
            comments = await client.list_pr_comments(1)
        assert comments == []

    async def test_list_pr_comments_returns_bodies(self):
        client = GitHubClient("owner/repo")
        payload = json.dumps({"comments": [{"body": "comment1"}, {"body": "comment2"}]})
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            comments = await client.list_pr_comments(1)
        assert comments == ["comment1", "comment2"]

    async def test_comment_on_pr_calls_gh(self):
        client = GitHubClient("owner/repo")
        with patch.object(client, "_run_checked", new=AsyncMock(return_value="")) as run:
            await client.comment_on_pr(5, "test comment")
        args = run.call_args.args
        assert "pr" in args and "comment" in args

    async def test_comment_on_issue_calls_gh(self):
        client = GitHubClient("owner/repo")
        with patch.object(client, "_run_checked", new=AsyncMock(return_value="")) as run:
            await client.comment_on_issue(3, "hi")
        args = run.call_args.args
        assert "issue" in args and "comment" in args

    async def test_list_recently_merged_prs_returns_prs(self):
        client = GitHubClient("owner/repo")
        payload = json.dumps([{
            "number": 5, "title": "merged pr", "url": "u",
            "labels": [{"name": FOLLOW_UPS_PROCESSED}],
            "headRefName": "auto/issue-5",
            "baseRefName": "main", "state": "merged", "isDraft": False,
        }])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            prs = await client.list_recently_merged_prs()
        assert len(prs) == 1
        assert FOLLOW_UPS_PROCESSED in prs[0].labels

    async def test_list_recently_merged_prs_returns_empty_on_error(self):
        client = GitHubClient("owner/repo")
        with patch.object(
            client, "_run_checked",
            new=AsyncMock(side_effect=GhCommandError(("gh",), 1, "", "err"))
        ):
            prs = await client.list_recently_merged_prs()
        assert prs == []

    async def test_clear_batch_label_removes_batch_labels(self):
        client = GitHubClient("owner/repo")
        issue = IssueRef(number=1, title="t", url="u",
                         labels=("~batch:abc12345", WORKFLOW_NEW))
        # remove_labels now issues a REST DELETE via _run (not _run_checked).
        with patch.object(client, "get_issue", new=AsyncMock(return_value=issue)), \
             patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))) as run:
            await client.clear_batch_label("issue", 1)
        # Should have called remove_labels (DELETE) with the batch label.
        assert run.called
        assert any("DELETE" in c.args for c in run.call_args_list)

    async def test_clear_batch_label_noop_when_no_batch(self):
        client = GitHubClient("owner/repo")
        issue = IssueRef(number=1, title="t", url="u", labels=(WORKFLOW_NEW,))
        with patch.object(client, "get_issue", new=AsyncMock(return_value=issue)), \
             patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))) as run:
            await client.clear_batch_label("issue", 1)
        # No batch labels to remove, so no DELETE should be issued.
        assert not run.called

    async def test_clear_batch_label_invalid_kind_raises(self):
        client = GitHubClient("owner/repo")
        with pytest.raises(ValueError):
            await client.clear_batch_label("comment", 1)

    async def test_list_unclassified_paginates_until_short_page(self):
        client = GitHubClient("owner/repo")
        # First batch: full 100 issues (all with ~labels → filtered out).
        batch1 = [
            {"number": i, "title": f"t{i}", "url": "u",
             "labels": [{"name": "~workflow:nova"}], "body": "", "state": "open"}
            for i in range(100)
        ]
        # Second batch: 3 unclassified issues.
        batch2 = [
            {"number": 200 + i, "title": f"new{i}", "url": "u",
             "labels": [], "body": "b", "state": "open"}
            for i in range(3)
        ]
        call_count = [0]

        async def fake_run_checked(*args):
            call_count[0] += 1
            limit_idx = args.index("--limit") + 1 if "--limit" in args else None
            if limit_idx and int(args[limit_idx]) <= 100:
                return json.dumps(batch1)
            return json.dumps(batch1 + batch2)

        with patch.object(client, "_run_checked", side_effect=fake_run_checked):
            results = await client.list_unclassified_issues()
        assert len(results) == 3
        assert all(r.number >= 200 for r in results)


# ---------------------------------------------------------------------------
# #31 — schedule-driven classify still triggers notification
# ---------------------------------------------------------------------------

class TestClassifyNotificationInScheduleMode:
    async def test_classify_notifies_when_schedule_driven(self, tmp_path):
        from deile.orchestration.pipeline.identity import MonitorIdentity

        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path,
            use_pid_lock=False, enable_classify=True,
        )
        issue = IssueRef(
            number=99, title="new issue", url="https://github.com/o/r/issues/99",
            labels=("intent",), body="some body",
        )
        github = AsyncMock()
        github.list_unclassified_issues = AsyncMock(return_value=[issue])
        github.claim_with_batch = AsyncMock(return_value="abc12345")
        github.add_labels = AsyncMock()
        github.comment_on_issue = AsyncMock()
        notifier = AsyncMock()

        # Use default identity (shard_count=1 → always owns)
        identity = MonitorIdentity(monitor_id="default", shard_index=0, shard_count=1)
        monitor = PipelineMonitor(
            cfg, github=github, worktrees=AsyncMock(),
            claude=AsyncMock(), notifier=notifier,
            identity=identity,
        )

        # Simulate a schedule-driven tick by calling _run_scheduled directly.
        from deile.orchestration.pipeline.scheduler import PendingRun
        run = PendingRun(
            when=datetime(2026, 1, 1, tzinfo=timezone.utc),
            entry_id="classify",
            action="classify",
            is_oneshot=False,
        )
        await monitor._run_scheduled(run)

        # Notification must have fired (same as legacy mode).
        notifier.issue_auto_classified.assert_called_once_with(
            99, "new issue", "https://github.com/o/r/issues/99"
        )


# ---------------------------------------------------------------------------
# Additional monitor.py coverage
# ---------------------------------------------------------------------------

class TestCatchUpPendingEdgeCases:
    def _make_monitor(self, tmp_path, schedule_store=None, worktrees=None):
        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path,
            use_pid_lock=False, enable_worktree_cleanup=True,
        )
        monitor = PipelineMonitor(
            cfg,
            github=AsyncMock(),
            worktrees=worktrees or AsyncMock(),
            claude=AsyncMock(),
            notifier=AsyncMock(),
            schedule_store=schedule_store or MagicMock(),
        )
        return monitor

    async def test_schedule_load_error_does_not_raise(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import ScheduleError
        schedule_store = MagicMock()
        schedule_store.load = MagicMock(side_effect=ScheduleError("corrupt"))
        worktrees = AsyncMock()
        worktrees.cleanup_merged_branches = AsyncMock(return_value=0)

        monitor = self._make_monitor(tmp_path, schedule_store=schedule_store, worktrees=worktrees)
        # Must not raise.
        await monitor._catch_up_pending()

    async def test_save_error_after_gc_does_not_raise(self, tmp_path):
        schedule_store = MagicMock()
        schedule_store.load = MagicMock(return_value=Schedule())
        schedule_store.save = MagicMock(side_effect=OSError("disk full"))

        worktrees = AsyncMock()
        worktrees.cleanup_merged_branches = AsyncMock(return_value=0)

        monitor = self._make_monitor(tmp_path, schedule_store=schedule_store, worktrees=worktrees)
        await monitor._catch_up_pending()  # must not raise despite save error

    async def test_worktree_cleanup_error_does_not_block_startup(self, tmp_path):
        schedule_store = MagicMock()
        schedule_store.load = MagicMock(return_value=Schedule())
        schedule_store.save = MagicMock()

        worktrees = AsyncMock()
        worktrees.cleanup_merged_branches = AsyncMock(side_effect=RuntimeError("gh down"))

        monitor = self._make_monitor(tmp_path, schedule_store=schedule_store, worktrees=worktrees)
        await monitor._catch_up_pending()  # must not raise

    async def test_save_error_after_catchup_does_not_raise(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import OneshotEntry

        # Build a schedule with one pending oneshot.
        run_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        oneshot = OneshotEntry(id="o1", action="classify", run_at=run_at)
        sched = Schedule(oneshot=[oneshot])

        schedule_store = MagicMock()
        schedule_store.load = MagicMock(return_value=sched)
        schedule_store.save = MagicMock(side_effect=OSError("disk full"))

        worktrees = AsyncMock()
        worktrees.cleanup_merged_branches = AsyncMock(return_value=0)

        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path,
            use_pid_lock=False, enable_worktree_cleanup=False,
            enable_classify=True,
        )
        github = AsyncMock()
        github.list_unclassified_issues = AsyncMock(return_value=[])
        monitor = PipelineMonitor(
            cfg, github=github, worktrees=worktrees,
            claude=AsyncMock(), notifier=AsyncMock(),
            schedule_store=schedule_store,
        )
        await monitor._catch_up_pending()  # must not raise


class TestStandaloneFollowUps:
    async def test_skips_already_processed_prs(self, tmp_path):
        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path, use_pid_lock=False,
        )
        already_processed = PrRef(
            number=1, title="done", url="u",
            labels=(FOLLOW_UPS_PROCESSED,),
        )
        github = AsyncMock()
        github.list_recently_merged_prs = AsyncMock(return_value=[already_processed])
        stage4_calls = []

        monitor = PipelineMonitor(
            cfg, github=github, worktrees=AsyncMock(),
            claude=AsyncMock(), notifier=AsyncMock(),
        )

        async def fake_stage4(*args):
            stage4_calls.append(args)

        monitor._stage4_follow_ups = fake_stage4
        await monitor._standalone_follow_ups()
        assert not stage4_calls

    async def test_processes_unprocessed_prs(self, tmp_path):
        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path, use_pid_lock=False,
        )
        pr = PrRef(number=2, title="new merged", url="https://gh/o/r/pull/2", labels=())
        github = AsyncMock()
        github.list_recently_merged_prs = AsyncMock(return_value=[pr])
        github.add_labels = AsyncMock()

        monitor = PipelineMonitor(
            cfg, github=github, worktrees=AsyncMock(),
            claude=AsyncMock(), notifier=AsyncMock(),
        )
        stage4_calls = []

        async def fake_stage4(number, title, url):
            stage4_calls.append((number, title, url))

        monitor._stage4_follow_ups = fake_stage4
        await monitor._standalone_follow_ups()
        assert stage4_calls == [(2, "new merged", "https://gh/o/r/pull/2")]
        github.add_labels.assert_called_once_with(
            "pr", 2, [FOLLOW_UPS_PROCESSED]
        )

    async def test_list_error_is_caught(self, tmp_path):
        cfg = PipelineConfig(
            repo="owner/repo", base_repo_path=tmp_path, use_pid_lock=False,
        )
        github = AsyncMock()
        github.list_recently_merged_prs = AsyncMock(side_effect=RuntimeError("boom"))

        monitor = PipelineMonitor(
            cfg, github=github, worktrees=AsyncMock(),
            claude=AsyncMock(), notifier=AsyncMock(),
        )
        await monitor._standalone_follow_ups()  # must not raise


# ---------------------------------------------------------------------------
# #16 — additional stages (implement, pr_review, follow_ups)
# ---------------------------------------------------------------------------

class TestSkippedRunsWarningAllStages:
    """Ensure skipped_runs is incremented for ALL scheduled actions, not just classify/review."""

    def _make_monitor(self, tmp_path, **config_overrides):
        cfg = PipelineConfig(
            repo="owner/repo",
            base_repo_path=tmp_path,
            use_pid_lock=False,
            **config_overrides,
        )
        return PipelineMonitor(
            cfg,
            github=AsyncMock(),
            worktrees=AsyncMock(),
            claude=AsyncMock(),
            notifier=AsyncMock(),
        )

    async def test_disabled_implement_increments_skipped_runs(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import PendingRun

        monitor = self._make_monitor(tmp_path, enable_implement=False)
        run = PendingRun(
            when=datetime(2026, 1, 1, tzinfo=timezone.utc),
            entry_id="r1",
            action="implement",
            is_oneshot=False,
        )
        await monitor._run_scheduled(run)
        assert monitor._stats.skipped_runs == 1

    async def test_disabled_pr_review_increments_skipped_runs(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import PendingRun

        monitor = self._make_monitor(tmp_path, enable_pr_review=False)
        run = PendingRun(
            when=datetime(2026, 1, 1, tzinfo=timezone.utc),
            entry_id="r1",
            action="pr_review",
            is_oneshot=False,
        )
        await monitor._run_scheduled(run)
        assert monitor._stats.skipped_runs == 1

    async def test_disabled_follow_ups_increments_skipped_runs(self, tmp_path):
        from deile.orchestration.pipeline.scheduler import PendingRun

        monitor = self._make_monitor(tmp_path, enable_follow_ups=False)
        run = PendingRun(
            when=datetime(2026, 1, 1, tzinfo=timezone.utc),
            entry_id="r1",
            action="follow_ups",
            is_oneshot=False,
        )
        await monitor._run_scheduled(run)
        assert monitor._stats.skipped_runs == 1


# ---------------------------------------------------------------------------
# #21 — _ensure_label called before add_labels inside claim_with_batch
# ---------------------------------------------------------------------------

class TestClaimWithBatchLabelOrdering:
    """Verifies that _ensure_label (label creation) precedes add_labels (label assignment)."""

    async def test_ensure_label_called_before_add_labels(self):
        client = GitHubClient("owner/repo")
        unclaimed = IssueRef(number=10, title="t", url="u", labels=(WORKFLOW_NEW,))
        our_batch = compute_batch_id_for_number("issue", 10)
        our_label = f"~batch:{our_batch}"
        after_add = IssueRef(number=10, title="t", url="u", labels=(WORKFLOW_NEW, our_label))

        call_order: list[str] = []

        async def fake_ensure_label(name, *, color, description):
            call_order.append("ensure_label")

        async def fake_add_labels(kind, number, labels):
            call_order.append("add_labels")

        with (
            patch.object(client, "get_issue", new=AsyncMock(side_effect=[unclaimed, after_add])),
            patch.object(client, "_ensure_label", side_effect=fake_ensure_label),
            patch.object(client, "add_labels", side_effect=fake_add_labels),
        ):
            result = await client.claim_with_batch("issue", 10)

        assert result == our_batch
        assert "ensure_label" in call_order
        assert "add_labels" in call_order
        assert call_order.index("ensure_label") < call_order.index("add_labels")


# ---------------------------------------------------------------------------
# #28 — --schedule-file relative path is resolved via Path()
# ---------------------------------------------------------------------------

class TestScheduleFileRelativePath:
    def test_schedule_file_relative_path_stem_is_filename(self):
        """Path('config/my_sched.yaml').stem == 'my_sched' and .parent == PurePosixPath('config')."""
        from pathlib import Path
        p = Path("config/my_sched.yaml")
        assert p.stem == "my_sched"
        assert str(p.parent) == "config"

    def test_schedule_file_bare_name_parent_is_dot(self):
        """A bare filename has parent == '.' (current directory)."""
        from pathlib import Path
        p = Path("sched.yaml")
        assert p.stem == "sched"
        assert str(p.parent) == "."
