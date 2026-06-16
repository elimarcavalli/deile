"""Tests for the dual-forge auth wiring in ``infra/k8s/wrapper.py`` (issue #297).

The wrapper materialises tokens into git/gh/glab config files on disk and
removes them from ``os.environ`` so subprocesses never see them in
``/proc/self/environ``. These tests exercise every combination of present
tokens (GH-only, GL-only, both, neither) without touching real ``gh`` /
``glab`` binaries — the configs are pure file writes the tests can read
back.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def wrapper_mod(tmp_path, monkeypatch):
    """Import ``infra/k8s/wrapper.py`` as a module and pin its HOME to tmp.

    The wrapper script is not a regular package (no ``__init__.py`` in
    ``infra/k8s/``), so we load it dynamically. ``HOME`` is repointed at a
    pristine tmp dir so each test starts with empty creds files.
    """
    repo_root = Path(__file__).resolve().parents[3]
    wrapper_path = repo_root / "infra" / "k8s" / "wrapper.py"
    spec = importlib.util.spec_from_file_location(
        "wrapper_under_test",
        str(wrapper_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wrapper_under_test"] = mod
    spec.loader.exec_module(mod)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Clear leftover tokens so a developer's real env doesn't leak in.
    for var in (
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "GL_TOKEN",
        "DEILE_GITHUB_HOST",
        "DEILE_GITLAB_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    yield mod, home


def test_no_tokens_writes_nothing(wrapper_mod):
    mod, home = wrapper_mod
    mod._setup_forge_credentials()
    assert not (home / ".git-credentials").exists()
    assert not (home / ".config" / "gh" / "hosts.yml").exists()
    assert not (home / ".config" / "glab-cli" / "config.yml").exists()


def test_github_only_writes_gh_files(wrapper_mod, monkeypatch):
    mod, home = wrapper_mod
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_TOKEN_VALUE_1234567890abcdef")
    mod._setup_forge_credentials()
    creds = (home / ".git-credentials").read_text()
    assert "github.com" in creds
    gh_yaml = (home / ".config" / "gh" / "hosts.yml").read_text()
    assert "github.com" in gh_yaml
    assert "oauth_token: ghp_test_TOKEN_VALUE_1234567890abcdef" in gh_yaml
    # GitLab config must NOT be created.
    assert not (home / ".config" / "glab-cli" / "config.yml").exists()
    # Token stripped from env after bootstrap.
    assert "GITHUB_TOKEN" not in os.environ


def test_gitlab_only_writes_glab_files(wrapper_mod, monkeypatch):
    mod, home = wrapper_mod
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test_TOKEN_VALUE_1234567890ab")
    mod._setup_forge_credentials()
    creds = (home / ".git-credentials").read_text()
    assert "gitlab.com" in creds
    glab_yaml = (home / ".config" / "glab-cli" / "config.yml").read_text()
    assert "gitlab.com" in glab_yaml
    assert "token: glpat-test_TOKEN_VALUE_1234567890ab" in glab_yaml
    assert not (home / ".config" / "gh" / "hosts.yml").exists()
    assert "GITLAB_TOKEN" not in os.environ


def test_dual_forge_writes_both(wrapper_mod, monkeypatch):
    mod, home = wrapper_mod
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_GH_TOKEN_1234567890abcdef1234")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-GL_TOKEN_1234567890abcdef")
    mod._setup_forge_credentials()
    creds = (home / ".git-credentials").read_text()
    # Both forge hosts present as distinct lines.
    lines = creds.strip().splitlines()
    assert any("@github.com" in line for line in lines)
    assert any("@gitlab.com" in line for line in lines)
    assert (home / ".config" / "gh" / "hosts.yml").exists()
    assert (home / ".config" / "glab-cli" / "config.yml").exists()
    assert "GITHUB_TOKEN" not in os.environ
    assert "GITLAB_TOKEN" not in os.environ


def test_custom_hosts_honoured(wrapper_mod, monkeypatch):
    mod, home = wrapper_mod
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_X_TOKEN_1234567890abcdef1234")
    monkeypatch.setenv("DEILE_GITHUB_HOST", "ghe.empresa.com")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-Y_TOKEN_1234567890abcdef")
    monkeypatch.setenv("DEILE_GITLAB_HOST", "gitlab.empresa.com")
    mod._setup_forge_credentials()
    creds = (home / ".git-credentials").read_text()
    assert "@ghe.empresa.com" in creds
    assert "@gitlab.empresa.com" in creds
    gh_yaml = (home / ".config" / "gh" / "hosts.yml").read_text()
    assert "ghe.empresa.com:" in gh_yaml
    glab_yaml = (home / ".config" / "glab-cli" / "config.yml").read_text()
    assert "gitlab.empresa.com:" in glab_yaml


def test_gl_token_alias_accepted(wrapper_mod, monkeypatch):
    mod, home = wrapper_mod
    monkeypatch.setenv("GL_TOKEN", "glpat-via_ALIAS_TOKEN_1234567890abc")
    mod._setup_forge_credentials()
    creds = (home / ".git-credentials").read_text()
    assert "@gitlab.com" in creds


def test_creds_file_permissions_owner_only(wrapper_mod, monkeypatch):
    mod, home = wrapper_mod
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_TOKEN_VALUE_1234567890abcdef")
    mod._setup_forge_credentials()
    creds = home / ".git-credentials"
    mode = creds.stat().st_mode & 0o777
    assert mode == 0o600, f"~/.git-credentials must be 0600 (got {oct(mode)})"


def test_idempotent_no_duplicate_lines_per_host(wrapper_mod, monkeypatch):
    mod, home = wrapper_mod
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_FIRST_TOKEN_1234567890abcdef1234")
    mod._setup_forge_credentials()
    # Re-run with a fresh token: the line for github.com must be REPLACED,
    # not appended (otherwise we end up with two oauth2:<...>@github.com
    # entries and git picks the wrong one).
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SECOND_TOKEN_1234567890abcdef12")
    mod._setup_forge_credentials()
    creds = (home / ".git-credentials").read_text()
    gh_lines = [line for line in creds.splitlines() if "@github.com" in line]
    assert len(gh_lines) == 1
    assert "SECOND" in gh_lines[0]


def test_legacy_shim_still_works(wrapper_mod, monkeypatch):
    """``_setup_git_credentials`` e ``_setup_gh_auth`` devem existir e delegar
    à função forge (nenhum call-site quebra)."""
    mod, home = wrapper_mod
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_LEGACY_TOKEN_1234567890abcdef12")
    # Chamadores legados podem chamar ambas — não deve quebrar e deve produzir config.
    mod._setup_gh_auth()
    mod._setup_git_credentials()
    assert (home / ".git-credentials").exists()


def test_setup_gh_auth_emits_deprecation_warning(wrapper_mod, monkeypatch):
    """``_setup_gh_auth`` deve emitir DeprecationWarning na primeira chamada."""
    import warnings

    mod, home = wrapper_mod
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_WARN_TOKEN_1234567890abcdefab")
    # Garante que o flag de aviso por processo seja False para este módulo recarregado.
    mod._setup_gh_auth_warned = False
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        mod._setup_gh_auth()
    assert any(
        issubclass(x.category, DeprecationWarning) for x in w
    ), "_setup_gh_auth deve emitir DeprecationWarning na 1ª chamada"
    assert any("_setup_forge_credentials" in str(x.message) for x in w)


def test_setup_gh_auth_warns_only_once(wrapper_mod, monkeypatch):
    """A segunda chamada de ``_setup_gh_auth`` na mesma sessão NÃO deve re-emitir."""
    import warnings

    mod, home = wrapper_mod
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_ONCE_TOKEN_1234567890abcdef12")
    mod._setup_gh_auth_warned = False
    # Primeira chamada — emite aviso.
    with warnings.catch_warnings(record=True) as w1:
        warnings.simplefilter("always")
        mod._setup_gh_auth()
    assert any(issubclass(x.category, DeprecationWarning) for x in w1)
    # Segunda chamada — NÃO deve re-emitir.
    with warnings.catch_warnings(record=True) as w2:
        warnings.simplefilter("always")
        mod._setup_gh_auth()
    assert not any(issubclass(x.category, DeprecationWarning) for x in w2)
