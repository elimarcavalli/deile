"""Tests for new GitHubClient methods added for issue #164 (gaps 2+4)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from deile.orchestration.pipeline.github_client import GhCommandError, GitHubClient


class TestListUnclassifiedPrs:
    async def test_returns_prs_without_pipeline_labels(self):
        client = GitHubClient("owner/name")
        payload = json.dumps(
            [
                {
                    "number": 1,
                    "title": "pr 1",
                    "url": "https://g/o/r/pull/1",
                    "labels": [],
                    "headRefName": "feat/x",
                    "baseRefName": "main",
                    "state": "open",
                    "isDraft": False,
                },
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_unclassified_prs()
        assert len(result) == 1
        assert result[0].number == 1

    async def test_filters_prs_with_pipeline_labels(self):
        client = GitHubClient("owner/name")
        payload = json.dumps(
            [
                {
                    "number": 2,
                    "title": "pr 2",
                    "url": "https://g/o/r/pull/2",
                    "labels": [{"name": "~review:pendente"}],
                    "headRefName": "b",
                    "baseRefName": "main",
                    "state": "open",
                    "isDraft": False,
                },
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_unclassified_prs()
        assert result == []

    async def test_filters_draft_prs(self):
        client = GitHubClient("owner/name")
        payload = json.dumps(
            [
                {
                    "number": 3,
                    "title": "pr 3",
                    "url": "https://g/o/r/pull/3",
                    "labels": [],
                    "headRefName": "b",
                    "baseRefName": "main",
                    "state": "open",
                    "isDraft": True,
                },
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_unclassified_prs()
        assert result == []

    async def test_returns_mixed_list_filtered_correctly(self):
        client = GitHubClient("owner/name")
        payload = json.dumps(
            [
                {
                    "number": 10,
                    "title": "clean pr",
                    "url": "https://g/o/r/pull/10",
                    "labels": [],
                    "headRefName": "feat/a",
                    "baseRefName": "main",
                    "state": "open",
                    "isDraft": False,
                },
                {
                    "number": 11,
                    "title": "labelled pr",
                    "url": "https://g/o/r/pull/11",
                    "labels": [{"name": "~workflow:new"}],
                    "headRefName": "feat/b",
                    "baseRefName": "main",
                    "state": "open",
                    "isDraft": False,
                },
                {
                    "number": 12,
                    "title": "draft pr",
                    "url": "https://g/o/r/pull/12",
                    "labels": [],
                    "headRefName": "feat/c",
                    "baseRefName": "main",
                    "state": "open",
                    "isDraft": True,
                },
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_unclassified_prs()
        assert len(result) == 1
        assert result[0].number == 10

    async def test_gh_error_propagates(self):
        client = GitHubClient("owner/name")
        with patch.object(
            client,
            "_run_checked",
            new=AsyncMock(side_effect=GhCommandError(("pr", "list"), 1, "", "err")),
        ):
            with pytest.raises(GhCommandError):
                await client.list_unclassified_prs()


class TestListIssueCommentsSince:
    async def test_returns_comments_after_since(self):
        client = GitHubClient("owner/name")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        payload = json.dumps(
            [
                {
                    "id": 42,
                    "body": "hello @deile-one",
                    "html_url": "https://github.com/o/r/issues/1#issuecomment-42",
                    "issue_url": "https://api.github.com/repos/o/r/issues/1",
                    "user": {"login": "alice"},
                }
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_issue_comments_since(since)
        assert len(result) == 1
        assert result[0].comment_id == 42
        assert result[0].author == "alice"
        assert result[0].kind == "issue"

    async def test_returns_empty_on_empty_list(self):
        client = GitHubClient("owner/name")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(client, "_run_checked", new=AsyncMock(return_value="[]")):
            result = await client.list_issue_comments_since(since)
        assert result == []

    async def test_returns_empty_on_gh_error(self):
        client = GitHubClient("owner/name")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(
            client,
            "_run_checked",
            new=AsyncMock(side_effect=GhCommandError(("api",), 1, "", "err")),
        ):
            result = await client.list_issue_comments_since(since)
        assert result == []

    async def test_skips_malformed_items(self):
        client = GitHubClient("owner/name")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        payload = json.dumps(
            [
                {"no_id_field": "bad"},
                {
                    "id": 7,
                    "body": "good comment",
                    "html_url": "https://github.com/o/r/issues/2#c7",
                    "issue_url": "https://api.github.com/repos/o/r/issues/2",
                    "user": {"login": "bob"},
                },
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_issue_comments_since(since)
        assert len(result) == 1
        assert result[0].comment_id == 7

    async def test_issue_url_field_populated(self):
        client = GitHubClient("owner/name")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        issue_url = "https://api.github.com/repos/o/r/issues/5"
        payload = json.dumps(
            [
                {
                    "id": 99,
                    "body": "msg",
                    "html_url": "https://github.com/o/r/issues/5#c99",
                    "issue_url": issue_url,
                    "user": {"login": "carol"},
                }
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_issue_comments_since(since)
        assert result[0].issue_url == issue_url


class TestListPrReviewCommentsSince:
    async def test_returns_pr_review_comments(self):
        client = GitHubClient("owner/name")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        payload = json.dumps(
            [
                {
                    "id": 99,
                    "body": "@deile-one revise isso",
                    "html_url": "https://github.com/o/r/pull/5#discussion_r99",
                    "pull_request_url": "https://api.github.com/repos/o/r/pulls/5",
                    "user": {"login": "bob"},
                }
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_pr_review_comments_since(since)
        assert len(result) == 1
        assert result[0].kind == "pr_review"
        assert result[0].author == "bob"

    async def test_issue_url_set_to_pull_request_url(self):
        client = GitHubClient("owner/name")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        pr_url = "https://api.github.com/repos/o/r/pulls/8"
        payload = json.dumps(
            [
                {
                    "id": 55,
                    "body": "review comment",
                    "html_url": "https://github.com/o/r/pull/8#discussion_r55",
                    "pull_request_url": pr_url,
                    "user": {"login": "dave"},
                }
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_pr_review_comments_since(since)
        assert result[0].issue_url == pr_url

    async def test_returns_empty_on_gh_error(self):
        client = GitHubClient("owner/name")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(
            client,
            "_run_checked",
            new=AsyncMock(side_effect=GhCommandError(("api",), 1, "", "err")),
        ):
            result = await client.list_pr_review_comments_since(since)
        assert result == []

    async def test_skips_malformed_items(self):
        client = GitHubClient("owner/name")
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        payload = json.dumps(
            [
                {"no_id_field": "bad"},
                {
                    "id": 22,
                    "body": "valid",
                    "html_url": "https://github.com/o/r/pull/3#discussion_r22",
                    "pull_request_url": "https://api.github.com/repos/o/r/pulls/3",
                    "user": {"login": "eve"},
                },
            ]
        )
        with patch.object(client, "_run_checked", new=AsyncMock(return_value=payload)):
            result = await client.list_pr_review_comments_since(since)
        assert len(result) == 1
        assert result[0].comment_id == 22
