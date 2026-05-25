"""Tests for :func:`deile.orchestration.forge.cli_renderer.render_brief_cmds`."""

from __future__ import annotations

import pytest

from deile.orchestration.forge.cli_renderer import render_brief_cmds


def test_github_starts_with_gh(github_config):
    cmds = render_brief_cmds(github_config, number=42, branch="auto/issue-42", main="main")
    assert cmds["clone_cmd"].startswith("gh ")
    assert cmds["create_pr_cmd"].startswith("gh pr create")
    assert cmds["view_issue_cmd"].startswith("gh issue view 42")
    assert cmds["merge_cmd"].startswith("gh api -X PUT")
    assert cmds["pr_url_pattern"] == "https://github.com/owner/repo/pull/42"
    assert cmds["pr_noun"] == "PR"
    assert cmds["forge_cli"] == "gh"
    assert cmds["forge_name"] == "GitHub"


def test_gitlab_starts_with_glab(gitlab_config):
    cmds = render_brief_cmds(gitlab_config, number=7, branch="auto/issue-7", main="main")
    assert cmds["clone_cmd"].startswith("glab ")
    assert cmds["create_pr_cmd"].startswith("glab mr create")
    assert cmds["view_issue_cmd"].startswith("glab issue view 7")
    assert cmds["merge_cmd"].startswith("glab api -X PUT")
    assert cmds["pr_url_pattern"] == "https://gitlab.com/group/project/-/merge_requests/7"
    assert cmds["pr_noun"] == "MR"
    assert cmds["forge_cli"] == "glab"
    assert cmds["forge_name"] == "GitLab"


def test_gitlab_nested_path_in_url(gitlab_nested_config):
    cmds = render_brief_cmds(
        gitlab_nested_config, number=99, branch="b", main="main",
    )
    assert cmds["pr_url_pattern"] == (
        "https://gitlab.com/group/subgroup/project/-/merge_requests/99"
    )
    assert cmds["issue_url_pattern"] == (
        "https://gitlab.com/group/subgroup/project/-/issues/99"
    )


def test_gitlab_uses_cached_project_id_when_available():
    """When the GitLab adapter has resolved the numeric ID, the renderer
    must use it (cheaper REST URL than the URL-encoded path)."""
    from deile.orchestration.forge.base import ForgeConfig, ForgeKind
    cfg = ForgeConfig(
        kind=ForgeKind.GITLAB,
        host="gitlab.com",
        project_path="g/p",
        cli_path="/usr/bin/glab",
        project_id="12345",
    )
    cmds = render_brief_cmds(cfg, number=1, branch="b", main="main")
    # The merge command MUST address by numeric id, not by encoded path.
    assert "projects/12345/merge_requests" in cmds["merge_cmd"]


def test_self_hosted_gitlab_url_uses_host(gitlab_selfhosted_config):
    cmds = render_brief_cmds(
        gitlab_selfhosted_config, number=1, branch="b", main="main",
    )
    assert cmds["pr_url_pattern"].startswith("https://gitlab.empresa.com/")


def test_self_hosted_github_url_uses_host(gh_enterprise_config):
    cmds = render_brief_cmds(
        gh_enterprise_config, number=1, branch="b", main="main",
    )
    assert cmds["pr_url_pattern"].startswith("https://ghe.empresa.com/")


def test_review_post_cmd_documents_both_event_types(gitlab_config):
    """The GitLab review_post placeholder must explain APPROVE vs REQUEST_CHANGES
    since the GL CLI uses two different verbs (no single ``--event`` flag)."""
    cmds = render_brief_cmds(gitlab_config, number=1, branch="b", main="main")
    assert "glab mr approve" in cmds["review_post_cmd"]
    assert "glab mr revoke" in cmds["review_post_cmd"]


def test_github_review_post_cmd_uses_event_flag(github_config):
    cmds = render_brief_cmds(github_config, number=1, branch="b", main="main")
    assert "event=<EVENT>" in cmds["review_post_cmd"]


@pytest.mark.parametrize("template_name", [
    "feature_request.md", "bug_report.md", "intent.md", "refactor_proposal.md",
])
def test_fetch_template_cmd_includes_template_name(github_config, template_name):
    cmds = render_brief_cmds(
        github_config, number=1, branch="b", main="main",
        issue_template=template_name,
    )
    assert template_name in cmds["fetch_template_cmd"]


def test_gitlab_fetch_template_uses_correct_path(gitlab_config):
    cmds = render_brief_cmds(
        gitlab_config, number=1, branch="b", main="main",
        issue_template="feature_request.md",
    )
    # GitLab keeps templates under ``.gitlab/issue_templates/`` — the URL
    # path is encoded with ``%2F`` inside the REST endpoint.
    assert ".gitlab%2Fissue_templates%2Ffeature_request.md" in cmds["fetch_template_cmd"]
