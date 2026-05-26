"""Tests for :class:`deile.orchestration.forge.ForgeRouter`."""

from __future__ import annotations

import pytest

from deile.orchestration.forge import (ForgeDetectionError, ForgeKind,
                                       ForgeRouter, GitHubForge, GitLabForge)


@pytest.fixture
def router(monkeypatch):
    # Make ``shutil.which`` always succeed so ``discover_cli`` does not
    # crash on hosts that lack ``gh``/``glab``.
    import shutil
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/fake/bin/{name}",
    )
    return ForgeRouter()


def test_router_routes_github_url(router):
    client = router.route(url="https://github.com/owner/repo/pull/1")
    assert isinstance(client, GitHubForge)
    assert client.kind is ForgeKind.GITHUB
    assert client.project_path == "owner/repo"


def test_router_routes_gitlab_url(router):
    client = router.route(url="https://gitlab.com/group/project/-/merge_requests/1")
    assert isinstance(client, GitLabForge)
    assert client.kind is ForgeKind.GITLAB
    assert client.project_path == "group/project"


def test_router_caches_per_host_repo(router):
    c1 = router.route(url="https://github.com/owner/repo/pull/1")
    c2 = router.route(url="https://github.com/owner/repo/issues/2")
    assert c1 is c2, "router must cache one client per (host, project)"


def test_router_separates_different_projects(router):
    a = router.route(url="https://github.com/owner/repo-a/pull/1")
    b = router.route(url="https://github.com/owner/repo-b/pull/1")
    assert a is not b


def test_router_mixed_session_github_and_gitlab(router):
    gh = router.route(url="https://github.com/owner/repo/pull/1")
    gl = router.route(url="https://gitlab.com/group/project/-/merge_requests/1")
    assert isinstance(gh, GitHubForge)
    assert isinstance(gl, GitLabForge)


def test_router_raises_on_unknown_host(router):
    with pytest.raises(ForgeDetectionError):
        router.route(url="https://gitea.example.com/o/r/issues/1")


def test_router_requires_project_or_url(router):
    with pytest.raises(ValueError):
        router.route()


def test_router_clear(router):
    a = router.route(url="https://github.com/owner/repo/pull/1")
    router.clear()
    b = router.route(url="https://github.com/owner/repo/pull/1")
    assert a is not b
