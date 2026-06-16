"""Tests for the forge-agnostic dataclasses (:mod:`deile.orchestration.forge.refs`)."""

from __future__ import annotations

from deile.orchestration.forge import (
    CommentRef,
    IssueRef,
    MentionTrigger,
    PrRef,
    compute_batch_id_for_number,
)


def test_issue_ref_from_gh_json():
    payload = {
        "number": 42,
        "title": "fix the thing",
        "url": "https://github.com/o/r/issues/42",
        "labels": [{"name": "bug"}, {"name": "~workflow:nova"}],
        "body": "Steps to reproduce…",
        "state": "open",
        "author": {"login": "alice"},
    }
    issue = IssueRef.from_gh_json(payload)
    assert issue.number == 42
    assert issue.title == "fix the thing"
    assert issue.labels == ("bug", "~workflow:nova")
    assert issue.author == "alice"


def test_issue_ref_from_gl_json_uses_iid_and_description():
    payload = {
        "iid": 7,
        "title": "feat: add gitlab support",
        "web_url": "https://gitlab.com/g/p/-/issues/7",
        "labels": ["enhancement", "~workflow:nova"],  # GL emits bare strings
        "description": "Markdown body…",
        "state": "opened",
        "author": {"username": "bob"},
    }
    issue = IssueRef.from_gl_json(payload)
    assert issue.number == 7
    assert issue.url.endswith("/-/issues/7")
    assert issue.body == "Markdown body…"
    # "opened" is normalised to the canonical "open" so the pipeline does
    # not branch on which forge produced the ref.
    assert issue.state == "open"
    assert issue.author == "bob"


def test_pr_ref_from_gh_json():
    payload = {
        "number": 11,
        "title": "feat: x",
        "url": "https://github.com/o/r/pull/11",
        "labels": [],
        "headRefName": "feat/x",
        "baseRefName": "main",
        "state": "open",
        "isDraft": False,
    }
    pr = PrRef.from_gh_json(payload)
    assert pr.head_ref == "feat/x"
    assert pr.base_ref == "main"
    assert pr.is_draft is False


def test_pr_ref_from_gl_json_normalises_draft_signals():
    # The MR has the draft boolean explicitly true.
    explicit = PrRef.from_gl_json(
        {
            "iid": 5,
            "title": "feat: y",
            "web_url": "https://gitlab.com/g/p/-/merge_requests/5",
            "labels": [],
            "source_branch": "feat/y",
            "target_branch": "main",
            "state": "opened",
            "draft": True,
        }
    )
    assert explicit.is_draft is True

    # No draft flag, but title prefixed with "Draft:" — same outcome.
    title_only = PrRef.from_gl_json(
        {
            "iid": 6,
            "title": "Draft: refactor that thing",
            "web_url": "https://gitlab.com/g/p/-/merge_requests/6",
            "labels": [],
            "source_branch": "refactor/thing",
            "target_branch": "main",
            "state": "opened",
        }
    )
    assert title_only.is_draft is True

    # No draft signal at all → False.
    clean = PrRef.from_gl_json(
        {
            "iid": 8,
            "title": "feat: ready",
            "web_url": "x",
            "labels": [],
            "source_branch": "feat",
            "target_branch": "main",
            "state": "opened",
        }
    )
    assert clean.is_draft is False


def test_pr_ref_gl_state_normalisation():
    pr = PrRef.from_gl_json(
        {
            "iid": 1,
            "title": "x",
            "web_url": "u",
            "labels": [],
            "source_branch": "a",
            "target_branch": "main",
            "state": "merged",
        },
        default_state="open",
    )
    assert pr.state == "merged"


def test_mention_trigger_dedup_key_consistent_across_role():
    """An assignee + a comment on the SAME issue share one dedup key."""
    issue = IssueRef.from_gh_json(
        {
            "number": 42,
            "title": "x",
            "url": "x",
            "labels": [],
            "body": "",
            "state": "open",
            "author": {"login": "alice"},
        }
    )
    comment = CommentRef(
        comment_id=1,
        body="@deile-one halp",
        html_url="x/issues/42#c-1",
        issue_url="x",
        author="alice",
        kind="issue",
    )
    by_assignee = MentionTrigger(trigger_type="assignee", issue=issue)
    by_comment = MentionTrigger(trigger_type="comment", comment=comment)
    assert by_assignee.dedup_key == by_comment.dedup_key == "issue:42"


def test_mention_trigger_target_number_from_comment_url():
    comment = CommentRef(
        comment_id=99,
        body="x",
        html_url="https://github.com/o/r/issues/77#issuecomment-1",
        issue_url="x",
        author="bob",
        kind="issue",
    )
    trigger = MentionTrigger(trigger_type="comment", comment=comment)
    assert trigger.target_number == 77


def test_compute_batch_id_deterministic_and_distinct_by_kind():
    a = compute_batch_id_for_number("issue", 1)
    b = compute_batch_id_for_number("issue", 1)
    c = compute_batch_id_for_number("pr", 1)
    assert a == b
    assert a != c
    assert len(a) == 8


def test_labels_tolerate_mixed_shapes():
    """GH ``--json labels`` emits objects; ``gh api --jq`` sometimes emits bare strings."""
    obj_form = IssueRef.from_gh_json(
        {
            "number": 1,
            "title": "",
            "url": "",
            "labels": [{"name": "x"}, {"name": "y"}],
            "body": "",
            "state": "open",
            "author": {},
        }
    )
    str_form = IssueRef.from_gh_json(
        {
            "number": 1,
            "title": "",
            "url": "",
            "labels": ["x", "y"],
            "body": "",
            "state": "open",
            "author": {},
        }
    )
    assert obj_form.labels == str_form.labels == ("x", "y")
