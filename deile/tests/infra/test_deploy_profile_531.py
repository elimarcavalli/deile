"""Tests for issue #531 — DEILE must boot without deilebot.

Covers:
  - D1: profile eixo único — sem novo flag, full ⇔ includes_bot.
  - D2: implicit full + no token → fallback to claude-only with warn (AC4).
  - D2: explicit full + no token → error preserved, exit≠0 (AC6).
  - D3: claude-only deployments == (deile-pipeline, deile-worker, claude-worker).
  - _k8s_up_resolve_profile explicit flag: cli --profile full → explicit=True.
  - _k8s_up_resolve_profile env var DEILE_K8S_DEPLOY_PROFILE=full → explicit=True.
  - _k8s_up_resolve_profile default → explicit=False.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import deploy  # noqa: E402


# ---------------------------------------------------------------------------
# D1 — profile eixo único
# ---------------------------------------------------------------------------

class TestD1ProfileAxis:
    def test_bot_only_in_full(self):
        assert deploy.DeploymentProfile("full").includes_bot is True
        assert deploy.DeploymentProfile("claude-only").includes_bot is False
        assert deploy.DeploymentProfile("pipeline-only").includes_bot is False

    def test_discord_required_only_in_full(self):
        assert deploy.DeploymentProfile("full").requires_discord is True
        assert deploy.DeploymentProfile("claude-only").requires_discord is False
        assert deploy.DeploymentProfile("pipeline-only").requires_discord is False


# ---------------------------------------------------------------------------
# D2/D3 — explicit flag tracking
# ---------------------------------------------------------------------------

class TestExplicitFlag:
    def test_cli_profile_flag_is_explicit(self, monkeypatch):
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)
        _, explicit = deploy._k8s_up_resolve_profile(["--profile", "full"], {})
        assert explicit is True

    def test_env_var_profile_is_explicit(self, monkeypatch):
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)
        _, explicit = deploy._k8s_up_resolve_profile([], {"DEILE_K8S_DEPLOY_PROFILE": "full"})
        assert explicit is True

    def test_os_environ_profile_is_explicit(self, monkeypatch):
        monkeypatch.setenv("DEILE_K8S_DEPLOY_PROFILE", "full")
        _, explicit = deploy._k8s_up_resolve_profile([], {})
        assert explicit is True

    def test_implicit_default_is_not_explicit(self, monkeypatch):
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)
        _, explicit = deploy._k8s_up_resolve_profile([], {})
        assert explicit is False

    def test_invalid_cli_flag_counts_as_explicit(self, monkeypatch):
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)
        p, explicit = deploy._k8s_up_resolve_profile(["--profile", "bogus"], {})
        assert p.name == "full"
        assert explicit is True

    def test_invalid_env_counts_as_explicit(self, monkeypatch):
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)
        p, explicit = deploy._k8s_up_resolve_profile([], {"DEILE_K8S_DEPLOY_PROFILE": "bogus"})
        assert p.name == "full"
        assert explicit is True


# ---------------------------------------------------------------------------
# D3 — claude-only deployments
# ---------------------------------------------------------------------------

class TestD3ClaudeOnlyDeployments:
    def test_claude_only_deployments(self):
        p = deploy.DeploymentProfile("claude-only")
        assert set(p.deployments) == {"deile-pipeline", "deile-worker", "claude-worker"}

    def test_claude_only_no_deilebot(self):
        p = deploy.DeploymentProfile("claude-only")
        assert "deilebot" not in p.deployments
        assert "deile-shell" not in p.deployments


# ---------------------------------------------------------------------------
# AC4 — implicit full + no token → fallback to claude-only with warn + exit 0
# ---------------------------------------------------------------------------

def _minimal_k8s_up_mocks(monkeypatch, tmp_path, *, discord_token="", extra_args=None):
    """Patch k8s_up down to just the profile/token gate.

    Returns (args, warn_calls, err_calls) so tests can assert on UI output.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ANTHROPIC_API_KEY=test-key\n"
        f"DEILE_BOT_DISCORD_TOKEN={discord_token}\n"
        f"DEILE_BOT_AUTH_TOKEN=tok\n"
        f"DEILE_WORKER_BEARER_TOKEN=tok\n"
        f"PIPELINE_STATUS_BEARER_TOKEN=tok\n"
    )

    warn_calls = []
    err_calls = []

    monkeypatch.setattr(deploy, "ENV_FILE", env_file)
    monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda yes: True)
    monkeypatch.setattr(deploy, "_kubectl", lambda: "kubectl")
    monkeypatch.setattr(deploy, "_assert_bearer_sync", lambda *a, **kw: None)
    monkeypatch.setattr(deploy, "_apply_secret", lambda *a, **kw: True)
    monkeypatch.setattr(deploy, "_run", lambda *a, **kw: 0)
    monkeypatch.setattr(deploy, "_apply_apiserver_egress_netpol", lambda *a, **kw: None)
    monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
    monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

    # Capture UI output for assertions
    original_warn = deploy.ui.warn
    original_err = deploy.ui.err
    monkeypatch.setattr(deploy.ui, "warn", lambda msg: warn_calls.append(msg) or original_warn(msg))
    monkeypatch.setattr(deploy.ui, "err", lambda msg: err_calls.append(msg) or original_err(msg))

    args = {
        "k8s_namespace": None,
        "yes": True,
        "dry_run": False,
        "extra": extra_args or [],
    }
    return args, warn_calls, err_calls


class TestAC4ImplicitFullFallback:
    def test_implicit_full_no_token_falls_back_to_claude_only(self, monkeypatch, tmp_path):
        args, warn_calls, err_calls = _minimal_k8s_up_mocks(
            monkeypatch, tmp_path, discord_token=""
        )
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)

        result = deploy.k8s_up(args)

        assert result == 0, "deve terminar com exit 0 (fallback, não erro)"
        assert not err_calls, f"nenhum erro esperado, mas houve: {err_calls}"
        # must warn about fallback
        full_warn = " ".join(warn_calls)
        assert "claude-only" in full_warn, "warn deve mencionar perfil claude-only"

    def test_implicit_full_no_token_warn_mentions_discord(self, monkeypatch, tmp_path):
        args, warn_calls, _ = _minimal_k8s_up_mocks(
            monkeypatch, tmp_path, discord_token=""
        )
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)

        deploy.k8s_up(args)

        full_warn = " ".join(warn_calls)
        assert "DEILE_BOT_DISCORD_TOKEN" in full_warn

    def test_implicit_full_with_token_keeps_full(self, monkeypatch, tmp_path):
        args, warn_calls, err_calls = _minimal_k8s_up_mocks(
            monkeypatch, tmp_path, discord_token="tok.123.abc"
        )
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)

        result = deploy.k8s_up(args)

        assert result == 0
        # no fallback warn should mention claude-only
        assert not any("claude-only" in w for w in warn_calls)


# ---------------------------------------------------------------------------
# AC6 — explicit full + no token → error preserved
# ---------------------------------------------------------------------------

class TestAC6ExplicitFullError:
    def test_explicit_full_no_token_returns_error(self, monkeypatch, tmp_path):
        args, warn_calls, err_calls = _minimal_k8s_up_mocks(
            monkeypatch, tmp_path, discord_token="",
            extra_args=["--profile", "full"],
        )
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)

        result = deploy.k8s_up(args)

        assert result == 1, "exit≠0 quando full explícito + sem token"
        assert err_calls, "deve emitir mensagem de erro"
        assert not any("claude-only" in w for w in warn_calls), \
            "não deve fazer fallback silencioso quando full é explícito"

    def test_explicit_full_via_env_no_token_returns_error(self, monkeypatch, tmp_path):
        args, warn_calls, err_calls = _minimal_k8s_up_mocks(
            monkeypatch, tmp_path, discord_token=""
        )
        monkeypatch.setenv("DEILE_K8S_DEPLOY_PROFILE", "full")

        result = deploy.k8s_up(args)

        assert result == 1
        assert err_calls
        assert not any("claude-only" in w for w in warn_calls)
