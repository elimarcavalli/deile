"""Render forge-specific CLI command snippets for the worker briefs.

Why this module exists:

The autonomous pipeline sends imperative briefs to the worker DEILE — long
prose prompts that include the exact ``gh ...`` commands the worker should
run inside its sandbox. With GitLab in the picture, those commands have to
become ``glab ...`` (different verbs, different flags, different REST paths).
Centralising them here gives:

- single source of truth (one place to edit per verb);
- the briefs become tooling-agnostic templates with ``{forge_*_cmd}`` slots;
- the renderer is testable in isolation (no LLM, no network) — every
  command snippet is a deterministic function of the forge + parameters.

The output dict keys mirror the placeholders used in the brief templates,
so adding a new brief is "drop a new placeholder + add a key here".
"""

from __future__ import annotations

from typing import Mapping

from deile.orchestration.forge.base import ForgeConfig, ForgeKind


def _gl_project_id(config: ForgeConfig) -> str:
    """Pick the cheapest project identifier for a GitLab REST URL.

    Uses the cached numeric ID if known (one byte vs many); otherwise falls
    back to the URL-encoded project path. Concrete adapters resolve and
    cache the numeric ID on first use, so by the time the worker brief is
    rendered the numeric path is typically already available.
    """
    return config.project_id or config.encoded_project_path


def render_brief_cmds(
    config: ForgeConfig,
    *,
    number: int,
    branch: str,
    main: str,
    issue_template: str = "feature_request.md",
    close_keyword: str = "Closes",
) -> Mapping[str, str]:
    """Return ``{placeholder: command}`` for *config*.

    The keys match the ``{forge_<key>_cmd}`` placeholders consumed by the
    brief templates in :mod:`deile.orchestration.pipeline.briefs`. Each
    value is a shell-ready string (already containing the parameters); the
    brief interpolates them as-is.

    ``issue_template`` only affects the ``fetch_template_cmd`` placeholder
    (used by the critique/refine briefs). The default matches the most
    common path; the renderer never hardcodes a fixed template name.

    ``close_keyword`` is the issue-closing verb baked into ``create_pr_cmd``'s
    body (``Closes #N``). Spikes — whose deliverable is measured evidence, not
    production code — pass ``"Refs"`` so the PR references the issue without
    auto-closing it on merge (a half-proven spike must never close its issue).
    """
    if config.kind is ForgeKind.GITHUB:
        return _github_cmds(
            project_path=config.project_path,
            host=config.host,
            number=number,
            branch=branch,
            main=main,
            issue_template=issue_template,
            close_keyword=close_keyword,
        )
    return _gitlab_cmds(
        project_path=config.project_path,
        host=config.host,
        project_id=_gl_project_id(config),
        number=number,
        branch=branch,
        main=main,
        issue_template=issue_template,
        close_keyword=close_keyword,
    )


def _github_cmds(
    *,
    project_path: str,
    host: str,
    number: int,
    branch: str,
    main: str,
    issue_template: str,
    close_keyword: str,
) -> Mapping[str, str]:
    """Concrete ``gh`` snippets — identical to the legacy literal commands
    that lived inline in the brief templates. Preserves byte-for-byte
    semantics so the GH regression suite stays green.
    """
    base = f"https://{host}"
    return {
        "clone_cmd": f"gh repo clone {project_path} repo",
        "view_issue_cmd": f"gh issue view {number} --repo {project_path} --comments",
        "create_pr_cmd": (
            f'gh pr create --repo {project_path} --base {main} --head {branch} '
            f'--title "<título coerente>" --body "<resumo>. {close_keyword} #{number}."'
        ),
        # Keyed by ``branch`` (not ``number``): in the implement flow the PR is
        # created fresh, so its number is unknown when the DoD block tells the
        # worker to mark it draft — but the head branch is always known. ``gh pr
        # ready`` accepts ``[<number> | <url> | <branch>]``, matching the branch
        # lookup already used by ``check_pr_cmd``.
        "mark_draft_cmd": f"gh pr ready {branch} --repo {project_path} --undo",
        "check_pr_cmd": f"gh pr view {branch} --repo {project_path} --json url -q .url",
        "checkout_pr_cmd": f"gh pr checkout {number}",
        "merge_cmd": (
            f"gh api -X PUT repos/{project_path}/pulls/{number}/merge "
            f"-f merge_method=merge"
        ),
        "merge_fallback_cmd": f"gh pr merge {number} --repo {project_path} --merge",
        "check_merged_cmd": f"gh pr view {number} --repo {project_path} --json merged -q .merged",
        "review_post_cmd": (
            f'gh api -X POST repos/{project_path}/pulls/{number}/reviews '
            f'-f event=<EVENT> -f body="<resumo>"'
        ),
        "comment_pr_cmd": f'gh pr comment {number} --repo {project_path} --body "<...>"',
        "comment_issue_cmd": f'gh issue comment {number} --repo {project_path} --body "<...>"',
        "edit_issue_title_cmd": (
            f'gh issue edit {number} --repo {project_path} --title "<novo título>"'
        ),
        "edit_issue_body_cmd": (
            f'gh issue edit {number} --repo {project_path} --body "<novo corpo>"'
        ),
        "fetch_template_cmd": (
            f"gh api repos/{project_path}/contents/.github/ISSUE_TEMPLATE/{issue_template} "
            f"--jq .content | base64 --decode"
        ),
        "list_pr_comments_cmd": f"gh api repos/{project_path}/pulls/{number}/comments",
        "view_pr_body_cmd": (
            f"gh pr view {number} --repo {project_path} "
            f"--json body,closingIssuesReferences"
        ),
        "view_pr_author_cmd": (
            f"gh pr view {number} --repo {project_path} --json author -q .author.login"
        ),
        "assign_user_cmd": (
            f'gh api -X POST repos/{project_path}/issues/{number}/assignees '
            f"-f 'assignees[]=<login>'"
        ),
        "create_issue_cmd": (
            f'gh issue create --repo {project_path} --title "<...>" --body "<...>" '
            f'--label "<tipo>"'
        ),
        "pr_url_pattern": f"{base}/{project_path}/pull/{number}",
        "issue_url_pattern": f"{base}/{project_path}/issues/{number}",
        # Forge identity (used in prose) — keeps the brief consistent.
        "forge_name": "GitHub",
        "forge_cli": "gh",
        "pr_noun": "PR",
        "pr_noun_lower": "pr",
    }


def _gitlab_cmds(
    *,
    project_path: str,
    host: str,
    project_id: str,
    number: int,
    branch: str,
    main: str,
    issue_template: str,
    close_keyword: str,
) -> Mapping[str, str]:
    """Concrete ``glab`` + GitLab REST snippets.

    The merge command uses the REST path (`projects/<id>/merge_requests/<iid>/merge`)
    because ``glab mr merge`` requires interactive confirmation in some
    versions; REST is the deterministic path. ``check_merged_cmd`` reads
    ``state`` (expected ``"merged"``).
    """
    base = f"https://{host}"
    return {
        "clone_cmd": f"glab repo clone {project_path} repo",
        "view_issue_cmd": f"glab issue view {number} -R {project_path} --comments",
        "create_pr_cmd": (
            f'glab mr create -R {project_path} --target-branch {main} '
            f'--source-branch {branch} -t "<título coerente>" '
            f'-d "<resumo>. {close_keyword} #{number}."'
        ),
        # Keyed by ``branch`` like ``check_pr_cmd`` above: the MR iid is unknown
        # when the implement DoD block marks the just-created MR draft, so the
        # REST path (which needs the iid) does not fit here. ``glab mr update``
        # accepts a branch selector and exposes ``--draft`` natively.
        "mark_draft_cmd": f"glab mr update {branch} -R {project_path} --draft",
        "check_pr_cmd": (
            f"glab mr view {branch} -R {project_path} -F json | jq -r .web_url"
        ),
        "checkout_pr_cmd": f"glab mr checkout {number}",
        "merge_cmd": (
            f"glab api -X PUT projects/{project_id}/merge_requests/{number}/merge "
            f"-f squash=false"
        ),
        "merge_fallback_cmd": f"glab mr merge {number} -R {project_path} --yes",
        "check_merged_cmd": (
            f"glab api projects/{project_id}/merge_requests/{number} | jq -r .state"
        ),
        "review_post_cmd": (
            f"# APPROVE: glab mr approve {number} -R {project_path}\n"
            f"# REQUEST_CHANGES: glab mr revoke {number} -R {project_path}\n"
            f'# Em ambos, segue: glab mr note {number} -R {project_path} '
            f'--message "<resumo>"'
        ),
        "comment_pr_cmd": (
            f'glab mr note {number} -R {project_path} --message "<...>"'
        ),
        "comment_issue_cmd": (
            f'glab issue note {number} -R {project_path} --message "<...>"'
        ),
        "edit_issue_title_cmd": (
            f'glab issue update {number} -R {project_path} --title "<novo título>"'
        ),
        "edit_issue_body_cmd": (
            f'glab issue update {number} -R {project_path} --description "<novo corpo>"'
        ),
        "fetch_template_cmd": (
            f"glab api projects/{project_id}/repository/files/"
            f".gitlab%2Fissue_templates%2F{issue_template}/raw?ref={main}"
        ),
        "list_pr_comments_cmd": (
            f"glab api projects/{project_id}/merge_requests/{number}/discussions"
        ),
        "view_pr_body_cmd": (
            f"glab api projects/{project_id}/merge_requests/{number} "
            f"| jq '{{description: .description, closes_issues: .closes_issues_url}}'"
        ),
        "view_pr_author_cmd": (
            f"glab api projects/{project_id}/merge_requests/{number} "
            f"| jq -r .author.username"
        ),
        # A API REST v4 do GitLab usa ``assignee_ids[]`` (semântica REPLACE
        # completo — ``add_assignee_ids`` NÃO existe). Empiricamente confirmado:
        # ``glab api -X PUT ... -f 'assignee_ids[]=N'`` retorna HTTP 400
        # ("assignee_ids ... are missing") porque glab envia params com ``[]``
        # no body form-encoded e o GitLab REST PUT requer o array na query
        # string. Solução: encodar ``[]`` como ``%5B%5D`` direto na URL.
        "assign_user_cmd": (
            f"glab api -X PUT 'projects/{project_id}/issues/{number}"
            f"?assignee_ids%5B%5D=<user_id>'"
        ),
        "create_issue_cmd": (
            f'glab issue create -R {project_path} -t "<...>" -d "<...>" '
            f'--label "<tipo>"'
        ),
        "pr_url_pattern": f"{base}/{project_path}/-/merge_requests/{number}",
        "issue_url_pattern": f"{base}/{project_path}/-/issues/{number}",
        "forge_name": "GitLab",
        "forge_cli": "glab",
        "pr_noun": "MR",
        "pr_noun_lower": "mr",
    }


__all__ = ["render_brief_cmds"]
