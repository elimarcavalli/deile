"""Branch-existence probe — covers Mistério #4 defensive guard.

The pipeline marks a PR ``~workflow:bloqueada`` when its head branch was
removed from the remote (force-deleted, squash-merge auto-delete, etc.).
This module verifies that :meth:`ForgeClient.branch_exists` returns the
right answer per forge against a scripted CLI:

* ``rc=0`` and no error → branch alive.
* ``rc!=0`` with explicit 404 body → branch absent.
* ``rc!=0`` with any other body → fail-open (returns True so a transient
  API hiccup never flags a healthy PR as orphan).

No real ``gh``/``glab`` binary is invoked; every ``_run`` call is
monkey-patched.
"""

from __future__ import annotations

from collections import deque
from typing import Tuple

import pytest

from deile.orchestration.forge import GitHubForge, GitLabForge
from deile.orchestration.forge.base import ForgeConfig, ForgeKind


@pytest.fixture
def fake_gh(monkeypatch):
    responses: "deque[Tuple[int, str, str]]" = deque()
    calls: list[tuple] = []
    cfg = ForgeConfig(
        kind=ForgeKind.GITHUB,
        host="github.com",
        project_path="owner/repo",
        cli_path="/usr/bin/gh",
    )
    forge = GitHubForge(cfg)

    async def fake_run(self, *args):
        calls.append(args)
        if not responses:
            return (0, "", "")
        return responses.popleft()

    monkeypatch.setattr(GitHubForge, "_run", fake_run)
    return forge, responses, calls


@pytest.fixture
def fake_glab(monkeypatch):
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
            return (0, "", "")
        return responses.popleft()

    monkeypatch.setattr(GitLabForge, "_run", fake_run)
    return forge, responses, calls


async def test_github_branch_exists_returns_true_on_rc0(fake_gh):
    forge, responses, _ = fake_gh
    responses.append((0, "", ""))
    assert await forge.branch_exists("auto/issue-1") is True


async def test_github_branch_exists_returns_false_on_404(fake_gh):
    forge, responses, _ = fake_gh
    responses.append((1, "", ""))  # first call (--silent)
    responses.append((1, "HTTP/2 404\n{\"message\":\"Not Found\"}", ""))  # -i
    assert await forge.branch_exists("auto/issue-vanished") is False


async def test_github_branch_exists_fail_open_on_unknown_error(fake_gh):
    forge, responses, _ = fake_gh
    responses.append((1, "", "network: timeout"))  # --silent
    responses.append((1, "HTTP/2 500\n{\"message\":\"oops\"}", ""))  # -i
    # Not 404 → fail-open (True) so transient errors never orphan a PR.
    assert await forge.branch_exists("auto/issue-1") is True


async def test_github_branch_exists_returns_false_on_empty_name(fake_gh):
    forge, _, _ = fake_gh
    assert await forge.branch_exists("") is False


async def test_gitlab_branch_exists_returns_true_on_rc0(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((0, "", ""))
    assert await forge.branch_exists("feature/x") is True


async def test_gitlab_branch_exists_returns_false_on_404(fake_glab):
    forge, responses, _ = fake_glab
    responses.append((1, "", ""))
    responses.append((1, "404 Not Found", ""))
    assert await forge.branch_exists("feature/vanished") is False


async def test_abc_branch_exists_default_is_true_fail_safe():
    """ABC default returns True so forges without an override never flag
    a PR as orphan-by-deleted-branch on a transient API condition.
    """
    from deile.orchestration.forge.base import ForgeClient

    # Inspect the ABC method directly via the GitHubForge MRO ancestor —
    # GitHubForge overrides it, but we want to assert the *base* behavior.
    base_method = ForgeClient.branch_exists
    # The method is async; call it through GitHubForge instance pointing at
    # the unbound base.
    cfg = ForgeConfig(
        kind=ForgeKind.GITHUB, host="github.com", project_path="o/r",
        cli_path="/usr/bin/gh",
    )
    forge = GitHubForge(cfg)
    # Call the ABC default directly with the instance as self.
    assert await base_method(forge, "anything") is True
    assert await base_method(forge, "") is True  # default does not gate on empty
