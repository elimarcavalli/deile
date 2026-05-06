"""Unit tests for GitHubClient — mocks the `gh` subprocess."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from deile.orchestration.pipeline.github_client import (GhCommandError,
                                                        GitHubClient,
                                                        IssueRef, PrRef,
                                                        compute_batch_id)
from deile.orchestration.pipeline.labels import (REVIEW_PENDING, WORKFLOW_NEW,
                                                 WORKFLOW_REVIEWED,
                                                 WORKFLOW_REVIEWING)


class TestComputeBatchId:
    def test_deterministic(self):
        assert compute_batch_id("hello") == compute_batch_id("hello")

    def test_changes_with_title(self):
        assert compute_batch_id("a") != compute_batch_id("b")

    def test_strips_whitespace(self):
        assert compute_batch_id("  hello  ") == compute_batch_id("hello")

    def test_returns_8_hex_chars(self):
        bid = compute_batch_id("anything")
        assert len(bid) == 8
        int(bid, 16)


class TestGitHubClientCtor:
    def test_rejects_invalid_repo(self):
        with pytest.raises(ValueError):
            GitHubClient("just-a-name")

    def test_accepts_owner_slash_name(self):
        client = GitHubClient("owner/name")
        assert client.repo == "owner/name"


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
        with patch.object(client, "_run_checked", new=AsyncMock(return_value="")) as run:
            await client.transition_issue(7, from_label=WORKFLOW_NEW, to_label=WORKFLOW_REVIEWING)
        # Two calls: remove + add
        assert run.call_count == 2
        remove_call = run.call_args_list[0].args
        add_call = run.call_args_list[1].args
        assert "--remove-label" in remove_call
        assert WORKFLOW_NEW in remove_call
        assert "--add-label" in add_call
        assert WORKFLOW_REVIEWING in add_call

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
             patch.object(client, "_run_checked", new=AsyncMock(return_value="")):
            bid = await client.claim_with_batch("issue", 5, "claim me")
        assert bid == compute_batch_id("claim me")

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

    async def test_claim_pr_uses_pr_list(self):
        client = GitHubClient("owner/name")
        pr = PrRef(number=7, title="t", url="u", labels=(REVIEW_PENDING,))
        with patch.object(client, "list_open_prs", new=AsyncMock(return_value=[pr])), \
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
        # `_run` returns rc=0 first call, rc=1 thereafter (already exists).
        rcs = iter([(0, "", ""), (1, "", "exists"), (1, "", ""), (1, "", ""),
                    (1, "", ""), (1, "", ""), (1, "", ""), (1, "", ""), (1, "", "")])
        with patch.object(client, "_run", new=AsyncMock(side_effect=lambda *a: next(rcs))):
            # Should not raise, regardless of existing/missing.
            await client.ensure_pipeline_labels()


class TestGhCommandError:
    def test_error_carries_metadata(self):
        err = GhCommandError(("gh", "issue", "list"), 2, "out", "err msg")
        assert err.returncode == 2
        assert "err msg" in str(err)
