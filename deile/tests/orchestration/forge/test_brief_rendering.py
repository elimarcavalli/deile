"""Tests that the worker briefs render correctly for both forges."""

from __future__ import annotations

import pytest

from deile.orchestration.pipeline.briefs import (
    _render_worker_critique_brief, _render_worker_decompose_brief,
    _render_worker_implement_brief, _render_worker_implement_resume_brief,
    _render_worker_pr_address_brief, _render_worker_refine_brief,
    _render_worker_review_brief, _render_worker_review_only_brief,
    _render_worker_review_resume_brief)


@pytest.fixture
def args():
    return {
        "repo": "owner/repo",
        "main": "main",
        "branch": "auto/issue-42",
        "number": 42,
        "title": "feat: x",
        "body": "Steps…",
    }


def test_implement_brief_github_uses_gh(args, github_config):
    out = _render_worker_implement_brief(**args, forge=github_config)
    assert "gh repo clone owner/repo repo" in out
    assert "gh pr create" in out
    assert "https://github.com/owner/repo/pull/42" in out
    assert "glab " not in out


def test_implement_brief_gitlab_uses_glab(args, gitlab_config):
    args["repo"] = "group/project"
    out = _render_worker_implement_brief(**args, forge=gitlab_config)
    assert "glab repo clone group/project repo" in out
    assert "glab mr create" in out
    assert "https://gitlab.com/group/project/-/merge_requests/42" in out
    # The pr noun must read "MR" in the GitLab brief.
    assert "MR" in out
    # And NO ``gh `` literal command may leak (the word "gh" may appear
    # in "github" elsewhere — we specifically guard the bare command).
    assert "gh pr create" not in out
    assert "gh issue view" not in out


def test_implement_brief_without_forge_defaults_to_github(args):
    # Legacy call site (no forge kwarg) must keep producing GH commands.
    out = _render_worker_implement_brief(**args)
    assert "gh pr create" in out


def test_review_brief_gitlab_merge_uses_glab(gitlab_config):
    out = _render_worker_review_brief(
        "group/project", "main", 7, forge=gitlab_config,
    )
    assert "glab api -X PUT" in out
    assert "merge_requests/7/merge" in out


def test_review_brief_github_merge_uses_gh(github_config):
    out = _render_worker_review_brief("owner/repo", "main", 7, forge=github_config)
    assert "gh api -X PUT repos/owner/repo/pulls/7/merge" in out


def test_implement_resume_brief_includes_progress_block(args, github_config):
    out = _render_worker_implement_resume_brief(**args, forge=github_config)
    assert "deile-progress.md" in out  # the journal note is embedded


def test_review_resume_brief_includes_progress_block(gitlab_config):
    out = _render_worker_review_resume_brief("g/p", "main", 5, forge=gitlab_config)
    assert "deile-progress.md" in out
    assert "glab" in out


def test_critique_brief_fetches_template_per_forge(gitlab_config):
    out = _render_worker_critique_brief(
        "g/p", 1, "T", "body", issue_type="feature",
        template="feature_request.md", forge=gitlab_config,
    )
    assert ".gitlab%2Fissue_templates%2Ffeature_request.md" in out
    # The GitHub fallback must not leak when forge is GitLab.
    assert ".github/ISSUE_TEMPLATE" not in out


def test_refine_brief_uses_glab_for_gitlab(gitlab_config):
    out = _render_worker_refine_brief(
        "g/p", 1, "T", "body", issue_type="bug",
        template="bug_report.md", forge=gitlab_config,
    )
    assert "glab issue update 1" in out
    assert "glab issue note" in out


def test_decompose_brief_uses_glab_create_for_gitlab(gitlab_config):
    out = _render_worker_decompose_brief(
        "g/p", 1, "T", "body", forge=gitlab_config,
    )
    assert "glab issue create" in out


def test_review_only_brief_review_post_per_forge(github_config, gitlab_config):
    gh_brief = _render_worker_review_only_brief("o/r", "main", 1, forge=github_config)
    gl_brief = _render_worker_review_only_brief("g/p", "main", 1, forge=gitlab_config)
    assert "gh api -X POST repos/" in gh_brief
    assert "glab mr approve" in gl_brief or "glab mr revoke" in gl_brief


def test_pr_address_brief_uses_correct_cli(github_config, gitlab_config):
    gh = _render_worker_pr_address_brief("o/r", "main", 5, forge=github_config)
    gl = _render_worker_pr_address_brief("g/p", "main", 5, forge=gitlab_config)
    assert "gh pr comment 5" in gh
    assert "glab mr note 5" in gl


def test_no_gh_literal_when_gitlab(args, gitlab_config):
    """Hardguard: with a GitLab forge, the worker brief must not contain a
    bare ``gh `` command anywhere — even in fallback prose."""
    args["repo"] = "group/project"
    for renderer in (
        _render_worker_implement_brief,
        _render_worker_implement_resume_brief,
    ):
        out = renderer(**args, forge=gitlab_config)
        for forbidden in ("gh pr create", "gh pr view", "gh issue view",
                          "gh issue edit", "gh issue comment",
                          "gh api -X", "gh pr comment", "gh pr checkout"):
            assert forbidden not in out, f"{renderer.__name__}: leaked {forbidden!r}"
