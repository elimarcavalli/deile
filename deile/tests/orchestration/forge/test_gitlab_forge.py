"""Tests for :class:`deile.orchestration.forge.gitlab_forge.GitLabForge`.

Adapter behaviour is exercised against a fake ``glab`` subprocess: every
call to :meth:`GitLabForge._run` / :meth:`_run_checked` is intercepted via
``monkeypatch`` so no real binary is ever invoked. The fixtures expose
helpers to script "next N responses" so each test reads as a small story
("given these responses, expect this call sequence").
"""

from __future__ import annotations

import json
from collections import deque
from typing import Tuple

import pytest

from deile.orchestration.forge import GitLabForge
from deile.orchestration.forge.base import (ForgeCommandError, ForgeConfig,
                                            ForgeKind, MergeBlocked,
                                            MergeBlockedByPipeline)


@pytest.fixture
def fake_glab(monkeypatch):
    """Returns ``(forge, responses)``. Append (rc, out, err) tuples to
    ``responses`` in the order the test expects them to be consumed.
    Each ``_run`` call pops the leftmost response.

    ``forge`` is built with a known ``ForgeConfig`` so no real ``glab``
    binary is needed.
    """
    responses: "deque[Tuple[int, str, str]]" = deque()
    calls: list[tuple] = []

    cfg = ForgeConfig(
        kind=ForgeKind.GITLAB,
        host="gitlab.com",
        project_path="group/project",
        cli_path="/usr/bin/glab",
    )
    forge = GitLabForge(cfg)

    async def fake_run(self, *args):
        calls.append(args)
        if not responses:
            return (0, "[]", "")
        return responses.popleft()

    monkeypatch.setattr(GitLabForge, "_run", fake_run)
    return forge, responses, calls


async def test_get_issue_uses_iid_endpoint(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, json.dumps({
        "iid": 7, "title": "x", "web_url": "u", "labels": [],
        "description": "d", "state": "opened", "author": {"username": "alice"},
    }), ""))
    issue = await forge.get_issue(7)
    assert issue.number == 7
    assert calls[0] == ("api", "projects/group%2Fproject/issues/7")


async def test_get_pr_filters_out_closed_mrs(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({
        "iid": 5, "title": "x", "web_url": "u", "labels": [],
        "source_branch": "b", "target_branch": "main", "state": "closed",
    }), ""))
    assert await forge.get_pr(5) is None


async def test_get_pr_open_returns_mr(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({
        "iid": 5, "title": "x", "web_url": "u", "labels": [],
        "source_branch": "b", "target_branch": "main", "state": "opened",
    }), ""))
    pr = await forge.get_pr(5)
    assert pr is not None
    assert pr.state == "open"
    assert pr.head_ref == "b"


async def test_list_open_prs_paginated(fake_glab):
    forge, responses, _ = fake_glab
    # First page: 100 items (full page) → triggers pagination.
    full_page = [
        {"iid": i, "title": f"mr{i}", "web_url": "u", "labels": [],
         "source_branch": "b", "target_branch": "main", "state": "opened"}
        for i in range(100)
    ]
    short_page = [
        {"iid": 100, "title": "mr100", "web_url": "u", "labels": [],
         "source_branch": "b", "target_branch": "main", "state": "opened"},
    ]
    responses.append((0, json.dumps(full_page), ""))
    responses.append((0, json.dumps(short_page), ""))
    prs = await forge.list_open_prs(limit=150)
    assert len(prs) == 101


async def test_add_labels_uses_put_endpoint(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, "{}", ""))
    await forge.add_labels("issue", 42, ["bug", "~workflow:nova"])
    assert calls[0][:3] == ("api", "-X", "PUT")
    assert calls[0][3] == "projects/group%2Fproject/issues/42"
    # GitLab takes comma-separated label names.
    assert "add_labels=bug,~workflow:nova" in calls[0]


async def test_remove_labels_uses_put(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, "{}", ""))
    await forge.remove_labels("pr", 1, ["foo"])
    assert calls[0][3] == "projects/group%2Fproject/merge_requests/1"
    assert "remove_labels=foo" in calls[0]


async def test_remove_labels_404_is_idempotent(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((1, "", "404 Not Found"))
    # Must not raise.
    await forge.remove_labels("issue", 42, ["nope"])


async def test_assign_issue_resolves_username_to_user_id(fake_glab):
    forge, responses, calls = fake_glab
    # 1st: user lookup. 2nd: PUT assignee_ids.
    responses.append((0, json.dumps([{"id": 123, "username": "alice"}]), ""))
    responses.append((0, "{}", ""))
    await forge.assign_issue(42, "alice")
    assert "username=alice" in calls[0]
    assert "assignee_ids[]=123" in calls[1]


async def test_assign_issue_handles_missing_user_gracefully(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, "[]", ""))  # user not found
    # Must not raise — courtesy signal.
    await forge.assign_issue(42, "ghost")


async def test_merge_pr_blocked_by_unmergeable_status(fake_glab):
    forge, responses, _ = fake_glab
    # The precheck reads the MR — return cannot_be_merged.
    responses.append((0, json.dumps({
        "iid": 5, "merge_status": "cannot_be_merged",
    }), ""))
    with pytest.raises(MergeBlocked):
        await forge.merge_pr(5)


async def test_merge_pr_blocked_by_pipeline_succeed_rule(fake_glab):
    forge, responses, _ = fake_glab
    # 1) precheck: OK.
    responses.append((0, json.dumps({"iid": 5, "merge_status": "can_be_merged"}), ""))
    # 2) actual merge: 405 with pipeline-related message.
    responses.append((
        1, "", "405 Method Not Allowed: Pipeline must succeed.",
    ))
    # 3) get_ci_status: MR with pipeline id.
    responses.append((0, json.dumps({"iid": 5, "head_pipeline": {"id": 99}}), ""))
    # 4) get_ci_status: pipeline running.
    responses.append((0, json.dumps({"status": "running"}), ""))
    with pytest.raises(MergeBlockedByPipeline) as exc_info:
        await forge.merge_pr(5)
    assert "pending" in str(exc_info.value).lower() or "running" in str(exc_info.value).lower()


async def test_merge_pr_success(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, json.dumps({"iid": 5, "merge_status": "can_be_merged"}), ""))
    responses.append((0, "{}", ""))
    await forge.merge_pr(5)
    # 1st call = precheck GET MR; 2nd call = PUT /merge.
    assert calls[1][:3] == ("api", "-X", "PUT")
    assert "merge_requests/5/merge" in calls[1][3]


async def test_get_ci_status_returns_none_when_no_pipeline(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({"iid": 5, "head_pipeline": None}), ""))
    assert await forge.get_ci_status(5) == "none"


@pytest.mark.parametrize("status, expected", [
    ("success", "passing"),
    ("failed", "failing"),
    ("canceled", "failing"),
    ("running", "pending"),
    ("pending", "pending"),
    ("manual", "pending"),
    ("skipped", "none"),
])
async def test_get_ci_status_normalises_gitlab_statuses(fake_glab, status, expected):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({"iid": 5, "head_pipeline": {"id": 1}}), ""))
    responses.append((0, json.dumps({"status": status}), ""))
    assert await forge.get_ci_status(5) == expected


async def test_resolve_project_id_caches_value(fake_glab):
    forge, responses, calls = fake_glab
    responses.append((0, json.dumps({"id": 9876, "default_branch": "trunk"}), ""))
    pid = await forge._resolve_project_id()
    assert pid == "9876"
    assert forge.config.project_id == "9876"
    # default_branch is captured as a side effect.
    assert forge.config.default_branch == "trunk"
    # Second call must NOT hit the API.
    pid2 = await forge._resolve_project_id()
    assert pid2 == "9876"
    assert len(calls) == 1


async def test_ensure_label_normalises_color_with_hash(fake_glab):
    """GitLab labels colors MUST be prefixed with '#' — adapter normalises."""
    forge, responses, calls = fake_glab
    responses.append((0, "{}", ""))
    await forge._ensure_label("~workflow:nova", color="0e8a16", description="x")
    flat = " ".join(calls[0])
    assert "color=#0e8a16" in flat


async def test_pr_reviewer_still_requested_reads_reviewers_array(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, json.dumps({
        "iid": 5, "reviewers": [{"username": "alice"}, {"username": "deile-one"}],
    }), ""))
    assert await forge.pr_reviewer_still_requested(5, "deile-one") is True


async def test_pr_reviewer_still_requested_fails_open(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((1, "", "boom"))  # API error
    # Must return False — never block work on a transient hiccup.
    assert await forge.pr_reviewer_still_requested(5, "deile-one") is False


async def test_invalid_project_path_raises():
    from deile.orchestration.forge.base import ForgeConfigError
    # A 1-segment path is not valid for GitLab (needs at least 2).
    with pytest.raises(ForgeConfigError):
        ForgeConfig(
            kind=ForgeKind.GITLAB,
            host="gitlab.com",
            project_path="single",
            cli_path="/usr/bin/glab",
        )


async def test_path_traversal_rejected():
    from deile.orchestration.forge.base import ForgeConfigError
    with pytest.raises(ForgeConfigError):
        ForgeConfig(
            kind=ForgeKind.GITLAB,
            host="gitlab.com",
            project_path="group/../etc",
            cli_path="/usr/bin/glab",
        )
