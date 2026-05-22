"""Unit tests for GitHubClient — mocks the `gh` subprocess."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from deile.orchestration.pipeline.github_client import (
    CommentRef, GhCommandError, GitHubClient, IssueRef, MentionTrigger, PrRef,
    compute_batch_id_for_number)
from deile.orchestration.pipeline.labels import (REVIEW_PENDING, WORKFLOW_NEW,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)


class TestGitHubClientCtor:
    def test_rejects_invalid_repo(self):
        with pytest.raises(ValueError):
            GitHubClient("just-a-name")

    def test_accepts_owner_slash_name(self):
        client = GitHubClient("owner/name")
        assert client.repo == "owner/name"

    @pytest.mark.parametrize("bad_repo", [
        "owner/..evil",       # path-traversal in name
        "../owner/name",      # leading traversal
        "owner/name/extra",   # too many segments
        "owner/",             # empty name
        "/name",              # empty owner
        "owner/name space",   # invalid char (space)
        "owner/name;rm",      # shell metachar
        "owner/name\nfoo",    # newline
    ])
    def test_rejects_path_traversal_and_bad_chars(self, bad_repo):
        with pytest.raises(ValueError):
            GitHubClient(bad_repo)


class TestListIssues:
    async def test_list_issues_with_label_parses_json(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {
                "number": 1,
                "title": "primeira",
                "url": "https://github.com/owner/name/issues/1",
                "labels": [{"name": WORKFLOW_NEW}],
                "body": "corpo",
                "state": "open",
            },
            {
                "number": 2,
                "title": "segunda",
                "url": "https://github.com/owner/name/issues/2",
                "labels": [{"name": WORKFLOW_NEW}, {"name": "~batch:abcd1234"}],
                "body": "outro corpo",
                "state": "open",
            },
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            issues = await client.list_issues_with_label(WORKFLOW_NEW)
        assert len(issues) == 2
        assert issues[0].number == 1
        assert issues[0].batch_id is None
        assert issues[1].batch_id == "abcd1234"

    async def test_list_issues_returns_empty_on_empty_output(self):
        client = GitHubClient("owner/name")
        with patch.object(client, "_run_checked", new=AsyncMock(return_value="")):
            issues = await client.list_issues_with_label(WORKFLOW_NEW)
        assert issues == []


class TestGetIssue:
    async def test_get_issue_parses_single_object(self):
        client = GitHubClient("owner/name")
        payload = json.dumps({
            "number": 9,
            "title": "test",
            "url": "https://github.com/owner/name/issues/9",
            "labels": [{"name": WORKFLOW_REVIEWED}],
            "body": "",
            "state": "open",
        })
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            issue = await client.get_issue(9)
        assert issue.number == 9
        assert WORKFLOW_REVIEWED in issue.labels


class TestTransitions:
    async def test_transition_issue_swaps_labels(self):
        client = GitHubClient("owner/name")
        # Labels now go through the REST issues/labels endpoint: remove is a
        # DELETE via _run (404-tolerant), add is a POST via _run_checked.
        with patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))) as run, \
             patch.object(client, "_run_checked", new=AsyncMock(return_value="")) as run_checked:
            await client.transition_issue(7, from_label=WORKFLOW_NEW, to_label=WORKFLOW_REVIEWING)
        remove_call = run.call_args_list[0].args
        assert "DELETE" in remove_call
        assert any(isinstance(a, str) and "/labels/" in a for a in remove_call)
        add_call = run_checked.call_args.args
        assert "POST" in add_call
        assert any(a == f"labels[]={WORKFLOW_REVIEWING}" for a in add_call)

    async def test_transition_skips_remove_when_from_label_none(self):
        client = GitHubClient("owner/name")
        with patch.object(client, "_run_checked", new=AsyncMock(return_value="")) as run:
            await client.transition_pr(7, from_label=None, to_label=REVIEW_PENDING)
        assert run.call_count == 1


class TestClaimWithBatch:
    async def test_claim_returns_batch_id_when_unclaimed(self):
        client = GitHubClient("owner/name")
        unclaimed = IssueRef(
            number=5,
            title="claim me",
            url="x",
            labels=(WORKFLOW_NEW,),
        )
        with patch.object(client, "get_issue", new=AsyncMock(return_value=unclaimed)), \
             patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))), \
             patch.object(client, "_run_checked", new=AsyncMock(return_value="")):
            bid = await client.claim_with_batch("issue", 5, "claim me")
        assert bid == compute_batch_id_for_number("issue", 5)

    async def test_claim_returns_none_when_already_claimed(self):
        client = GitHubClient("owner/name")
        claimed = IssueRef(
            number=5,
            title="claim me",
            url="x",
            labels=(WORKFLOW_NEW, "~batch:dead0000"),
        )
        with patch.object(client, "get_issue", new=AsyncMock(return_value=claimed)):
            bid = await client.claim_with_batch("issue", 5, "claim me")
        assert bid is None

    async def test_claim_pr_uses_pr_view(self):
        client = GitHubClient("owner/name")
        pr = PrRef(number=7, title="t", url="u", labels=(REVIEW_PENDING,))
        with patch.object(client, "get_pr", new=AsyncMock(return_value=pr)), \
             patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))), \
             patch.object(client, "_run_checked", new=AsyncMock(return_value="")):
            bid = await client.claim_with_batch("pr", 7, "t")
        assert bid is not None

    async def test_claim_rejects_invalid_kind(self):
        client = GitHubClient("owner/name")
        with pytest.raises(ValueError):
            await client.claim_with_batch("comment", 1, "x")


class TestEnsureLabels:
    async def test_creates_all_labels_idempotent(self):
        client = GitHubClient("owner/name")
        # `_run` always returns rc=0 (created) — robust regardless of label count.
        with patch.object(client, "_run", new=AsyncMock(return_value=(0, "", ""))):
            # Should not raise, regardless of existing/missing.
            await client.ensure_pipeline_labels()

    async def test_creates_blocked_label(self):
        # Resume feature (issue #254): ensure_pipeline_labels must create the
        # ~workflow:bloqueada label so the block flow can apply it.
        from deile.orchestration.pipeline.labels import WORKFLOW_BLOCKED
        client = GitHubClient("owner/name")
        created: list = []

        async def fake_run(*args):
            # ``label create <name> --repo ...`` — capture the label name.
            if args and args[0] == "label" and args[1] == "create":
                created.append(args[2])
            return (0, "", "")

        with patch.object(client, "_run", new=AsyncMock(side_effect=fake_run)):
            await client.ensure_pipeline_labels()
        assert WORKFLOW_BLOCKED in created


class TestGhCommandError:
    def test_error_carries_metadata(self):
        err = GhCommandError(("gh", "issue", "list"), 2, "out", "err msg")
        assert err.returncode == 2
        assert "err msg" in str(err)


class TestEnsureLabelOnClaim:
    """The batch lock label must be created on-demand by claim_with_batch."""

    async def test_claim_creates_batch_label_before_adding(self):
        from unittest.mock import AsyncMock, patch
        client = GitHubClient("owner/name")
        unclaimed = IssueRef(
            number=42, title="brand new", url="x", labels=(WORKFLOW_NEW,),
        )
        calls = []

        async def fake_run(*args):
            calls.append(args)
            return (0, "", "")

        async def fake_run_checked(*args):
            calls.append(args)
            return ""

        with patch.object(client, "get_issue", new=AsyncMock(return_value=unclaimed)), \
             patch.object(client, "_run", side_effect=fake_run), \
             patch.object(client, "_run_checked", side_effect=fake_run_checked):
            bid = await client.claim_with_batch("issue", 42, "brand new")

        assert bid is not None
        # The first call must be `label create ~batch:<bid>` via _run
        assert calls[0][0] == "label" and calls[0][1] == "create"
        assert calls[0][2].startswith("~batch:")

    async def test_claim_pr_creates_batch_label_before_adding(self):
        """For PRs, _ensure_label must be called BEFORE add_labels — same
        contract as for issues (tested above).  This test verifies call order
        using a spy that records every invocation."""
        client = GitHubClient("owner/name")
        unclaimed_pr = PrRef(
            number=11, title="pr title", url="u", labels=(REVIEW_PENDING,),
            head_ref="auto/issue-11",
        )
        calls = []

        async def fake_run(*args):
            calls.append(("_run", *args))
            return (0, "", "")

        async def fake_run_checked(*args):
            calls.append(("_run_checked", *args))
            return ""

        with patch.object(client, "get_pr", new=AsyncMock(return_value=unclaimed_pr)), \
             patch.object(client, "_run", side_effect=fake_run), \
             patch.object(client, "_run_checked", side_effect=fake_run_checked):
            bid = await client.claim_with_batch("pr", 11, "pr title")

        assert bid is not None

        # Locate the _ensure_label call (label create) and the add_labels call.
        ensure_idx = next(
            (i for i, c in enumerate(calls) if c[0] == "_run" and "label" in c and "create" in c),
            None,
        )
        # add_labels now POSTs to the REST issues/labels endpoint (avoids the
        # read:org scope that ``gh pr edit`` demands), so match the api call.
        add_idx = next(
            (
                i for i, c in enumerate(calls)
                if c[0] == "_run_checked" and "api" in c
                and any(isinstance(x, str) and x.endswith("/labels") for x in c)
            ),
            None,
        )
        assert ensure_idx is not None, f"_ensure_label not called; calls={calls}"
        assert add_idx is not None, f"add_labels not called; calls={calls}"
        assert ensure_idx < add_idx, (
            f"_ensure_label (idx={ensure_idx}) must precede add_labels (idx={add_idx})"
        )


# ----- MentionTrigger (issue #253) ----------------------------------------


class TestMentionTrigger:
    def test_dedup_key_groups_by_target(self):
        """Two triggers on the same issue share a dedup key."""
        issue = IssueRef(number=42, title="t", url="u", labels=())
        t1 = MentionTrigger(trigger_type="assignee", issue=issue)
        t2 = MentionTrigger(trigger_type="body", issue=issue)
        assert t1.dedup_key == t2.dedup_key == "issue:42"

    def test_dedup_key_different_targets(self):
        """Triggers on different issues have different dedup keys."""
        i1 = IssueRef(number=1, title="a", url="u", labels=())
        i2 = IssueRef(number=2, title="b", url="u", labels=())
        t1 = MentionTrigger(trigger_type="assignee", issue=i1)
        t2 = MentionTrigger(trigger_type="body", issue=i2)
        assert t1.dedup_key != t2.dedup_key

    def test_dedup_key_pr(self):
        """PR triggers produce 'pr:N' dedup key."""
        pr = PrRef(number=7, title="t", url="u", labels=())
        t = MentionTrigger(trigger_type="reviewer", pr=pr)
        assert t.dedup_key == "pr:7"

    def test_target_kind_from_issue(self):
        issue = IssueRef(number=1, title="t", url="u", labels=())
        t = MentionTrigger(trigger_type="assignee", issue=issue)
        assert t.target_kind == "issue"

    def test_target_kind_from_pr(self):
        pr = PrRef(number=1, title="t", url="u", labels=())
        t = MentionTrigger(trigger_type="reviewer", pr=pr)
        assert t.target_kind == "pr"

    def test_target_kind_from_issue_comment(self):
        comment = CommentRef(
            comment_id=1, body="x", html_url="https://github.com/o/r/issues/5#c1",
            issue_url="https://api.github.com/repos/o/r/issues/5",
            author="u", kind="issue",
        )
        t = MentionTrigger(trigger_type="comment", comment=comment)
        assert t.target_kind == "issue"

    def test_target_kind_from_pr_comment(self):
        comment = CommentRef(
            comment_id=2, body="x", html_url="https://github.com/o/r/pull/8#discussion",
            issue_url="https://api.github.com/repos/o/r/pulls/8",
            author="u", kind="pr_review",
        )
        t = MentionTrigger(trigger_type="comment", comment=comment)
        assert t.target_kind == "pr"

    def test_target_number_from_issue(self):
        issue = IssueRef(number=99, title="t", url="u", labels=())
        t = MentionTrigger(trigger_type="assignee", issue=issue)
        assert t.target_number == 99

    def test_target_number_from_pr(self):
        pr = PrRef(number=55, title="t", url="u", labels=())
        t = MentionTrigger(trigger_type="reviewer", pr=pr)
        assert t.target_number == 55

    def test_target_number_from_comment_url(self):
        comment = CommentRef(
            comment_id=3, body="x",
            html_url="https://github.com/o/r/issues/123#issuecomment-456",
            issue_url="https://api.github.com/repos/o/r/issues/123",
            author="u", kind="issue",
        )
        t = MentionTrigger(trigger_type="comment", comment=comment)
        assert t.target_number == 123

    def test_unknown_target_kind(self):
        """Trigger with no issue, pr, or comment returns 'unknown'."""
        t = MentionTrigger(trigger_type="assignee")
        assert t.target_kind == "unknown"
        assert t.target_number == 0


# ----- New GitHubClient methods (issue #253) ------------------------------


class TestListIssuesAssignedTo:
    async def test_parses_assigned_issues(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {"number": 10, "title": "bug", "url": "u",
             "labels": [], "body": "b", "state": "open"},
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            issues = await client.list_issues_assigned_to("deile-one")
        assert len(issues) == 1
        assert issues[0].number == 10

    async def test_gh_error_returns_empty(self):
        client = GitHubClient("owner/name")
        with patch.object(client, "_run_checked",
                          new=AsyncMock(side_effect=GhCommandError(("x",), 1, "", "err"))):
            issues = await client.list_issues_assigned_to("deile-one")
        assert issues == []


class TestListPrsAssignedTo:
    async def test_parses_assigned_prs(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {"number": 20, "title": "feat", "url": "u",
             "labels": [], "headRefName": "auto/issue-20",
             "baseRefName": "main", "state": "open", "isDraft": False},
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            prs = await client.list_prs_assigned_to("deile-one")
        assert len(prs) == 1
        assert prs[0].number == 20

    async def test_gh_error_returns_empty(self):
        client = GitHubClient("owner/name")
        with patch.object(client, "_run_checked",
                          new=AsyncMock(side_effect=GhCommandError(("x",), 1, "", "err"))):
            prs = await client.list_prs_assigned_to("deile-one")
        assert prs == []


class TestListPrsWithReviewRequests:
    async def test_parses_review_requested_prs(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {"number": 30, "title": "pr", "url": "u",
             "labels": [{"name": "bug"}],
             "headRefName": "feat/x", "baseRefName": "main",
             "state": "open", "isDraft": False},
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            prs = await client.list_prs_with_review_requests("deile-one")
        assert len(prs) == 1
        assert prs[0].number == 30

    async def test_single_object_normalized_to_list(self):
        """gh api --jq returns a single dict when 1 match; must be normalized."""
        client = GitHubClient("owner/name")
        payload = json.dumps({
            "number": 31, "title": "single", "url": "u",
            "labels": [{"name": "enhancement"}],
            "headRefName": "feat/y", "baseRefName": "main",
            "state": "open", "isDraft": False,
        })
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            prs = await client.list_prs_with_review_requests("deile-one")
        assert len(prs) == 1
        assert prs[0].number == 31

    async def test_string_labels_normalized(self):
        """String labels like ["bug", "feat"] must be normalized to [{name: ...}]."""
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {"number": 32, "title": "string labels", "url": "u",
             "labels": ["bug", "feat"],
             "headRefName": "feat/z", "baseRefName": "main",
             "state": "open", "isDraft": False},
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            prs = await client.list_prs_with_review_requests("deile-one")
        assert len(prs) == 1
        assert "bug" in prs[0].labels

    async def test_gh_error_returns_empty(self):
        client = GitHubClient("owner/name")
        with patch.object(client, "_run_checked",
                          new=AsyncMock(side_effect=GhCommandError(("x",), 1, "", "err"))):
            prs = await client.list_prs_with_review_requests("deile-one")
        assert prs == []


class TestSearchItemsMentioning:
    async def test_separates_issues_from_prs(self):
        client = GitHubClient("owner/name")
        payload = json.dumps([
            {"number": 5, "title": "issue", "url": "https://github.com/o/r/issues/5",
             "labels": [], "body": "@deile-one", "state": "open"},
            {"number": 6, "title": "pr", "url": "https://github.com/o/r/pull/6",
             "labels": [], "body": "@deile-one", "state": "open"},
        ])
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            issues, prs = await client.search_items_mentioning("@deile-one")
        assert len(issues) == 1
        assert issues[0].number == 5
        assert len(prs) == 1
        assert prs[0].number == 6

    async def test_gh_error_returns_empty(self):
        client = GitHubClient("owner/name")
        with patch.object(client, "_run_checked",
                          new=AsyncMock(side_effect=GhCommandError(("x",), 1, "", "err"))):
            issues, prs = await client.search_items_mentioning("@deile-one")
        assert issues == []
        assert prs == []
