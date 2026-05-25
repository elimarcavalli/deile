"""Shared fixtures for the forge-layer test suite.

The fixtures below build :class:`ForgeConfig` instances without touching the
filesystem (no real ``gh``/``glab`` binary required). They are the canonical
way every test that needs a ``ForgeClient`` should obtain its config.
"""

from __future__ import annotations

import pytest

from deile.orchestration.forge.base import ForgeConfig, ForgeKind


@pytest.fixture
def github_config() -> ForgeConfig:
    """A ready-to-use GitHub :class:`ForgeConfig` for cloud."""
    return ForgeConfig(
        kind=ForgeKind.GITHUB,
        host="github.com",
        project_path="owner/repo",
        cli_path="/usr/bin/gh",
    )


@pytest.fixture
def gitlab_config() -> ForgeConfig:
    """A ready-to-use GitLab :class:`ForgeConfig` for cloud."""
    return ForgeConfig(
        kind=ForgeKind.GITLAB,
        host="gitlab.com",
        project_path="group/project",
        cli_path="/usr/bin/glab",
    )


@pytest.fixture
def gitlab_nested_config() -> ForgeConfig:
    """A GitLab config with nested groups (3-level path)."""
    return ForgeConfig(
        kind=ForgeKind.GITLAB,
        host="gitlab.com",
        project_path="group/subgroup/project",
        cli_path="/usr/bin/glab",
    )


@pytest.fixture
def gitlab_selfhosted_config() -> ForgeConfig:
    """A GitLab config pointing at a self-hosted instance."""
    return ForgeConfig(
        kind=ForgeKind.GITLAB,
        host="gitlab.empresa.com",
        project_path="team/svc",
        cli_path="/usr/bin/glab",
    )


@pytest.fixture
def gh_enterprise_config() -> ForgeConfig:
    """A GitHub config for an Enterprise Server host."""
    return ForgeConfig(
        kind=ForgeKind.GITHUB,
        host="ghe.empresa.com",
        project_path="team/svc",
        cli_path="/usr/bin/gh",
    )
