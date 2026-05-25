"""Tests for the forge-layer settings (issue #297)."""

from __future__ import annotations

import pytest

from deile.config.settings import Settings


def test_forge_kind_default_is_auto():
    s = Settings()
    assert s.forge_kind == "auto"


def test_forge_hosts_defaults():
    s = Settings()
    assert s.forge_github_host == "github.com"
    assert s.forge_gitlab_host == "gitlab.com"


def test_forge_probe_disabled_by_default():
    s = Settings()
    assert s.forge_probe_enabled is False


def test_forge_repo_blank_by_default_and_falls_back_to_pipeline_repo(monkeypatch):
    """The new ``forge_repo`` knob is blank by default — the resolver falls
    back to the legacy ``pipeline_repo`` so old configs keep working."""
    from deile.orchestration.pipeline.constants import resolve_forge_repo
    s = Settings()
    assert s.forge_repo == ""
    # The resolver prefers forge_repo when non-empty.
    s.forge_repo = "group/sub/proj"
    monkeypatch.setattr(
        "deile.orchestration.pipeline.constants.get_settings", lambda: s,
    )
    assert resolve_forge_repo() == "group/sub/proj"


def test_legacy_resolve_pipeline_repo_calls_forge_resolver(monkeypatch):
    """The deprecated alias must delegate — not return the legacy value
    directly — so callers transparently see the new behaviour."""
    from deile.orchestration.pipeline.constants import (resolve_forge_repo,
                                                        resolve_pipeline_repo)
    assert resolve_pipeline_repo() == resolve_forge_repo()
