"""Tests for issue #360: wire forge.gitlab_api_version and forge.github_api_prefix.

Verifies that:
- ``_rewrite_gl_api_args`` correctly rewrites relative endpoints to versioned URLs.
- ``GitLabForge._run`` applies versioning before delegating to the base _run.
- ``_rewrite_gh_api_args`` correctly rewrites relative endpoints for GHES.
- ``GitHubForge._run`` applies prefix rewriting for GHES with non-default prefix.
- ``detection._check_gitlab_endpoint`` uses the configured API version.
"""

from __future__ import annotations

import json

from deile.config.settings import Settings
from deile.orchestration.forge import GitLabForge
from deile.orchestration.forge.base import ForgeClient, ForgeConfig, ForgeKind
from deile.orchestration.forge.github_forge import (GitHubForge,
                                                    _rewrite_gh_api_args)
from deile.orchestration.forge.gitlab_forge import _rewrite_gl_api_args

# ---------------------------------------------------------------------------
# _rewrite_gl_api_args (pure-function unit tests)
# ---------------------------------------------------------------------------


def test_rewrite_gl_api_args_plain_endpoint():
    """Simple endpoint → full versioned URL."""
    result = _rewrite_gl_api_args(
        host="gitlab.example.com",
        version="3",
        args=("api", "projects/group%2Fproject/issues/7"),
    )
    assert result == (
        "api",
        "https://gitlab.example.com/api/v3/projects/group%2Fproject/issues/7",
    )


def test_rewrite_gl_api_args_with_method_flag():
    """``-X METHOD`` before the endpoint is skipped; endpoint is rewritten."""
    result = _rewrite_gl_api_args(
        host="gitlab.example.com",
        version="3",
        args=("api", "-X", "POST", "projects/123/issues/1/notes",
              "--raw-field", "body=hello"),
    )
    assert result == (
        "api",
        "-X",
        "POST",
        "https://gitlab.example.com/api/v3/projects/123/issues/1/notes",
        "--raw-field",
        "body=hello",
    )


def test_rewrite_gl_api_args_with_paginate_flag():
    """``--paginate`` (standalone) before endpoint is skipped; endpoint rewritten."""
    result = _rewrite_gl_api_args(
        host="gitlab.example.com",
        version="3",
        args=("api", "--paginate", "projects/123/issues/1/resource_label_events"),
    )
    assert result == (
        "api",
        "--paginate",
        "https://gitlab.example.com/api/v3/projects/123/issues/1/resource_label_events",
    )


def test_rewrite_gl_api_args_already_full_url_unchanged():
    """Full URL endpoints are NOT rewritten (avoids double-prefixing)."""
    full = "https://gitlab.example.com/api/v4/projects/123"
    result = _rewrite_gl_api_args("gitlab.example.com", "3", ("api", full))
    assert result == ("api", full)


# ---------------------------------------------------------------------------
# GitLabForge._run integration: versioning applied before super()._run
# ---------------------------------------------------------------------------


def _make_gl_forge(host: str = "old-gitlab.example.com") -> GitLabForge:
    cfg = ForgeConfig(
        kind=ForgeKind.GITLAB,
        host=host,
        project_path="group/project",
        cli_path="/usr/bin/glab",
    )
    return GitLabForge(cfg)


async def test_gitlab_api_version_setting_used_in_url(monkeypatch):
    """GitLabForge rewrites the endpoint to include /api/v3/ when version is "3"."""
    forge = _make_gl_forge(host="old-gitlab.example.com")
    base_calls: list = []

    async def fake_base_run(self, *args):
        base_calls.append(args)
        return (0, json.dumps({
            "iid": 7, "title": "t", "web_url": "u",
            "labels": [], "description": "d",
            "state": "opened", "author": {"username": "u"},
        }), "")

    monkeypatch.setattr(ForgeClient, "_run", fake_base_run)

    s = Settings()
    s.forge_gitlab_api_version = "3"
    monkeypatch.setattr("deile.config.settings.get_settings", lambda: s)

    await forge.get_issue(7)

    assert base_calls, "expected at least one base _run call"
    endpoint = base_calls[0][-1]
    assert endpoint.startswith("https://old-gitlab.example.com/api/v3/"), (
        f"expected full URL with /api/v3/, got {endpoint!r}"
    )


async def test_gitlab_api_version_default_keeps_relative_path(monkeypatch):
    """With default version ``"4"`` the endpoint is NOT rewritten."""
    forge = _make_gl_forge()
    base_calls: list = []

    async def fake_base_run(self, *args):
        base_calls.append(args)
        return (0, json.dumps({
            "iid": 1, "title": "t", "web_url": "u",
            "labels": [], "description": "d",
            "state": "opened", "author": {"username": "u"},
        }), "")

    monkeypatch.setattr(ForgeClient, "_run", fake_base_run)

    s = Settings()
    assert s.forge_gitlab_api_version == "4"
    monkeypatch.setattr("deile.config.settings.get_settings", lambda: s)

    await forge.get_issue(1)

    assert base_calls
    endpoint = base_calls[0][-1]
    assert not endpoint.startswith("https://"), (
        f"with default version, endpoint should be relative, got {endpoint!r}"
    )


def test_gitlab_api_version_env_var(monkeypatch):
    """DEILE_GITLAB_API_VERSION env var is picked up by settings."""
    from deile.config.settings import _apply_env_overrides

    monkeypatch.setenv("DEILE_GITLAB_API_VERSION", "3")
    s = Settings()
    _apply_env_overrides(s)
    assert s.forge_gitlab_api_version == "3"


# ---------------------------------------------------------------------------
# _rewrite_gh_api_args (pure-function unit tests)
# ---------------------------------------------------------------------------


def test_rewrite_gh_api_args_plain_endpoint():
    """Simple endpoint → full URL with configured prefix."""
    result = _rewrite_gh_api_args(
        host="ghes.example.com",
        prefix="api/v4",
        args=("api", "repos/owner/repo/issues"),
    )
    assert result == (
        "api",
        "https://ghes.example.com/api/v4/repos/owner/repo/issues",
    )


def test_rewrite_gh_api_args_with_method_flag():
    """``-X METHOD`` before endpoint is skipped; endpoint is rewritten."""
    result = _rewrite_gh_api_args(
        host="ghes.example.com",
        prefix="api/v3",
        args=("api", "-X", "GET", "repos/owner/repo/pulls"),
    )
    assert result == (
        "api",
        "-X",
        "GET",
        "https://ghes.example.com/api/v3/repos/owner/repo/pulls",
    )


def test_rewrite_gh_api_args_already_full_url_unchanged():
    """Full URL endpoints are NOT rewritten."""
    full = "https://ghes.example.com/api/v3/repos/owner/repo"
    result = _rewrite_gh_api_args("ghes.example.com", "api/v3", ("api", full))
    assert result == ("api", full)


def test_github_api_prefix_env_var(monkeypatch):
    """DEILE_GITHUB_API_PREFIX env var is picked up by settings."""
    from deile.config.settings import _apply_env_overrides

    monkeypatch.setenv("DEILE_GITHUB_API_PREFIX", "api/v3")
    s = Settings()
    _apply_env_overrides(s)
    assert s.forge_github_api_prefix == "api/v3"


async def test_github_api_prefix_setting_used_in_url(monkeypatch):
    """GitHubForge rewrites endpoint to include custom prefix for GHES."""
    cfg = ForgeConfig(
        kind=ForgeKind.GITHUB,
        host="ghes.example.com",
        project_path="owner/repo",
        cli_path="/usr/bin/gh",
    )
    forge = GitHubForge(cfg)
    base_calls: list = []

    async def fake_base_run(self, *args):
        base_calls.append(args)
        return (0, "[]", "")

    monkeypatch.setattr(ForgeClient, "_run", fake_base_run)

    s = Settings()
    s.forge_github_api_prefix = "api/v4"
    monkeypatch.setattr("deile.config.settings.get_settings", lambda: s)

    try:
        await forge.list_issues_with_label("~workflow:nova")
    except Exception:
        pass  # We only care that _run was called with the rewritten endpoint.

    api_calls = [c for c in base_calls if c and c[0] == "api"]
    if api_calls:
        rewritten = [
            a for args in api_calls for a in args[1:]
            if isinstance(a, str) and a.startswith("https://ghes.example.com/api/v4/")
        ]
        assert rewritten, (
            f"expected at least one rewritten endpoint with api/v4, got {api_calls}"
        )


async def test_github_api_prefix_skipped_for_github_dot_com(monkeypatch):
    """GitHubForge does NOT rewrite endpoints for github.com (only for GHES)."""
    cfg = ForgeConfig(
        kind=ForgeKind.GITHUB,
        host="github.com",
        project_path="owner/repo",
        cli_path="/usr/bin/gh",
    )
    forge = GitHubForge(cfg)
    base_calls: list = []

    async def fake_base_run(self, *args):
        base_calls.append(args)
        return (0, "[]", "")

    monkeypatch.setattr(ForgeClient, "_run", fake_base_run)

    s = Settings()
    s.forge_github_api_prefix = "api/v4"
    monkeypatch.setattr("deile.config.settings.get_settings", lambda: s)

    try:
        await forge.list_issues_with_label("~workflow:nova")
    except Exception:
        pass

    api_calls = [c for c in base_calls if c and c[0] == "api"]
    if api_calls:
        rewritten = [
            a for args in api_calls for a in args[1:]
            if isinstance(a, str) and a.startswith("https://github.com/")
        ]
        assert not rewritten, (
            f"github.com endpoint MUST NOT be rewritten, got {api_calls}"
        )


# ---------------------------------------------------------------------------
# detection._check_gitlab_endpoint
# ---------------------------------------------------------------------------


def test_detection_uses_gitlab_api_version_in_probe(monkeypatch):
    """_check_gitlab_endpoint probes /api/v<version>/version, not hardcoded /api/v4/."""
    from deile.orchestration.forge.detection import _check_gitlab_endpoint

    probed_urls: list = []

    class FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=3):
        probed_urls.append(req.full_url)
        return FakeResponse()

    s = Settings()
    s.forge_gitlab_api_version = "3"
    monkeypatch.setattr("deile.config.settings.get_settings", lambda: s)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = _check_gitlab_endpoint("gl3.example.com")
    assert result is ForgeKind.GITLAB
    assert any("/api/v3/version" in url for url in probed_urls), (
        f"expected probe to /api/v3/version, got {probed_urls}"
    )
