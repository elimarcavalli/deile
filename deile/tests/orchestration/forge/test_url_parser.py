"""Tests for :mod:`deile.orchestration.forge.url_parser`."""

from __future__ import annotations

from deile.orchestration.forge import (
    ForgeKind,
    find_first_pr_url,
    find_last_pr_url,
    parse_forge_url,
)


def test_parse_forge_url_github_pr():
    u = parse_forge_url("https://github.com/owner/repo/pull/42")
    assert u is not None
    assert u.kind is ForgeKind.GITHUB
    assert u.host == "github.com"
    assert u.project_path == "owner/repo"
    assert u.target_kind == "pr"
    assert u.number == 42


def test_parse_forge_url_github_issue():
    u = parse_forge_url("https://github.com/owner/repo/issues/7")
    assert u is not None and u.target_kind == "issue" and u.number == 7


def test_parse_forge_url_gitlab_simple_mr():
    u = parse_forge_url("https://gitlab.com/g/p/-/merge_requests/9")
    assert u is not None
    assert u.kind is ForgeKind.GITLAB
    assert u.project_path == "g/p"
    assert u.target_kind == "pr"
    assert u.number == 9


def test_parse_forge_url_gitlab_nested_mr():
    u = parse_forge_url("https://gitlab.com/group/sub/proj/-/merge_requests/77")
    assert u is not None
    assert u.project_path == "group/sub/proj"
    assert u.number == 77


def test_parse_forge_url_gitlab_issue():
    u = parse_forge_url("https://gitlab.com/g/p/-/issues/3")
    assert u is not None and u.target_kind == "issue" and u.number == 3


def test_parse_forge_url_self_hosted_github_enterprise():
    u = parse_forge_url(
        "https://ghe.empresa.com/team/svc/pull/1",
        github_hosts=("ghe.empresa.com",),
    )
    assert u is not None
    assert u.kind is ForgeKind.GITHUB
    assert u.host == "ghe.empresa.com"


def test_parse_forge_url_self_hosted_gitlab():
    u = parse_forge_url(
        "https://gitlab.empresa.com/x/y/-/issues/3",
        gitlab_hosts=("gitlab.empresa.com",),
    )
    assert u is not None and u.kind is ForgeKind.GITLAB


def test_parse_forge_url_unknown_host_returns_none():
    # An unknown host MUST NOT be guessed — it returns None so the caller
    # fails fast instead of silently misrouting.
    assert parse_forge_url("https://gitea.example.com/o/r/issues/1") is None


def test_parse_forge_url_rejects_invalid_inputs():
    assert parse_forge_url("") is None
    assert parse_forge_url("not-a-url") is None
    assert parse_forge_url("ftp://github.com/o/r/pull/1") is None
    assert parse_forge_url(None) is None  # type: ignore[arg-type]


def test_parse_forge_url_handles_trailing_fragment():
    u = parse_forge_url("https://github.com/o/r/pull/42#issuecomment-1")
    assert u is not None and u.number == 42


def test_find_first_pr_url_returns_first_match():
    text = (
        "Veja a doc em https://github.com/o/r/issues/1 antes; "
        "minha PR é https://github.com/o/r/pull/77 e a próxima "
        "será https://github.com/o/r/pull/99."
    )
    assert find_first_pr_url(text) == "https://github.com/o/r/pull/77"


def test_find_last_pr_url_returns_last_match():
    """Catches the canonical use case: agent prints example URL early,
    real URL on the final line."""
    text = (
        "Exemplo de URL: https://github.com/o/r/pull/1\n"
        "...\n"
        "https://github.com/o/r/pull/99"
    )
    assert find_last_pr_url(text) == "https://github.com/o/r/pull/99"


def test_find_last_pr_url_mixed_forges():
    text = "github.com pr: https://github.com/o/r/pull/1 ; gitlab mr: https://gitlab.com/g/p/-/merge_requests/5"
    # The last URL is a GitLab MR — but the function must accept either as
    # "PR-like".
    assert find_last_pr_url(text) == "https://gitlab.com/g/p/-/merge_requests/5"


def test_find_last_pr_url_no_match():
    assert find_last_pr_url("nada aqui") is None
    assert find_last_pr_url("") is None


def test_url_strips_trailing_punctuation():
    text = "concluído em https://github.com/o/r/pull/3."
    assert find_first_pr_url(text) == "https://github.com/o/r/pull/3"
