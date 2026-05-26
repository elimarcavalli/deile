"""Tests for CLI binary discovery and the failure mode when it's absent."""

from __future__ import annotations

import pytest

from deile.orchestration.forge import ForgeCliNotFound, discover_cli


def test_discover_cli_returns_path_when_present(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/fake/bin/{name}")
    assert discover_cli("gh") == "/fake/bin/gh"
    assert discover_cli("glab") == "/fake/bin/glab"


def test_discover_cli_raises_when_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ForgeCliNotFound) as exc_info:
        discover_cli("glab")
    msg = str(exc_info.value)
    # The message MUST name the binary so the operator knows what to install.
    assert "glab" in msg
    # And must hint at where the install guide lives.
    assert "CLAUDE.md" in msg or "install" in msg.lower()


def test_github_forge_legacy_constructor_fails_fast_without_gh(monkeypatch):
    """Constructing GitHubForge with the legacy positional ``repo`` shape
    when ``gh`` is not on PATH must raise :class:`ForgeCliNotFound`
    immediately — not at first call. That keeps the failure obvious."""
    from deile.orchestration.forge import GitHubForge
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ForgeCliNotFound):
        GitHubForge("owner/repo")


def test_gitlab_forge_legacy_constructor_fails_fast_without_glab(monkeypatch):
    from deile.orchestration.forge import GitLabForge
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ForgeCliNotFound):
        GitLabForge("group/project")
