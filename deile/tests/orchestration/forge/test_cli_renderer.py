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


def test_create_pr_cmd_defaults_to_closes(github_config):
    """Default close_keyword keeps the legacy ``Closes #N`` (byte-for-byte)."""
    cmds = render_brief_cmds(github_config, number=42, branch="auto/issue-42", main="main")
    assert "Closes #42" in cmds["create_pr_cmd"]


def test_create_pr_cmd_refs_when_spike_keyword(github_config):
    """A spike passes ``close_keyword="Refs"`` so the PR never auto-closes the issue."""
    cmds = render_brief_cmds(
        github_config, number=42, branch="auto/issue-42", main="main",
        close_keyword="Refs",
    )
    assert "Refs #42" in cmds["create_pr_cmd"]
    assert "Closes #42" not in cmds["create_pr_cmd"]


def test_github_mark_draft_cmd(github_config):
    cmds = render_brief_cmds(github_config, number=42, branch="auto/issue-42", main="main")
    assert cmds["mark_draft_cmd"] == "gh pr ready 42 --repo owner/repo --undo"


def test_gitlab_close_keyword_and_draft(gitlab_config):
    cmds = render_brief_cmds(
        gitlab_config, number=7, branch="auto/issue-7", main="main", close_keyword="Refs",
    )
    assert "Refs #7" in cmds["create_pr_cmd"]
    assert "Closes #7" not in cmds["create_pr_cmd"]
    assert cmds["mark_draft_cmd"].startswith("glab mr update 7")
    assert "--draft" in cmds["mark_draft_cmd"]


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


def test_gitlab_assign_uses_assignee_ids_not_add_assignee_ids(gitlab_config):
    """O endpoint PUT /issues/:id do GitLab aceita ``assignee_ids[]``, NÃO
    ``add_assignee_ids`` (parâmetro inexistente na API REST v4 oficial).

    Empiricamente confirmado contra gitlab.com (2026-05-26): ``-f assignee_ids[]=N``
    no body retorna HTTP 400; o valor precisa ir na query string da URL como
    ``?assignee_ids%5B%5D=<id>``.
    """
    cmds = render_brief_cmds(gitlab_config, number=5, branch="b", main="main")
    # ``[]`` aparece encoded como ``%5B%5D`` na query string.
    assert "assignee_ids%5B%5D=<user_id>" in cmds["assign_user_cmd"]
    # Garante ausência da forma errada (que retorna 400 do GitLab).
    assert "add_assignee_ids" not in cmds["assign_user_cmd"]
    # Garante ausência da forma errada em body (``-f assignee_ids[]=``).
    assert "-f 'assignee_ids[]" not in cmds["assign_user_cmd"]
    assert "--raw-field 'assignee_ids[]" not in cmds["assign_user_cmd"]


def test_gitlab_assign_uses_query_string_not_body(gitlab_config):
    """Garante que ``assignee_ids`` vai na query string (``?…``), não no body.

    Bug empírico: ``glab api -X PUT -f assignee_ids[]=N`` envia no body
    form-encoded e o GitLab REST PUT rejeita com HTTP 400. A correção encoda
    ``[]`` em ``%5B%5D`` direto na URL.
    """
    cmds = render_brief_cmds(gitlab_config, number=5, branch="b", main="main")
    cmd = cmds["assign_user_cmd"]
    # URL deve conter ``?assignee_ids%5B%5D=`` (query string).
    assert "?assignee_ids%5B%5D=" in cmd
    # ``-f`` solto não deve aparecer como flag de corpo isolada (params via URL).
    parts = cmd.split()
    assert "-f" not in parts
    assert "--raw-field" not in parts
