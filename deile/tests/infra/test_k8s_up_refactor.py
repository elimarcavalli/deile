"""Tests for k8s_up refactoring (issue #354).

Covers:
  - DeploymentProfile class (profiles, properties)
  - _persist_env_key helper (update existing, append new, noop on missing file)
  - _ensure_persisted_token helper (use existing, generate+persist when absent)
  - _k8s_up_resolve_profile (CLI flag, .env, os.environ, default)
  - k8s_up behaviour:
      * pipeline-only profile does NOT require Discord token
      * full profile requires Discord token
      * GitLab token included in deile-secrets
      * pipeline-status-bearer Secret always created
      * auto-generated tokens persisted back to .env
      * profile-specific manifests applied
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import deploy  # noqa: E402


# ============================================================================
# DeploymentProfile
# ============================================================================

class TestDeploymentProfile:
    def test_valid_names_accepted(self):
        for name in ("pipeline-only", "full", "claude-only"):
            p = deploy.DeploymentProfile(name)
            assert p.name == name

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError, match="perfil inválido"):
            deploy.DeploymentProfile("unknown-profile")

    def test_pipeline_only_deployments(self):
        p = deploy.DeploymentProfile("pipeline-only")
        assert set(p.deployments) == {"deile-pipeline", "deile-worker"}
        assert "deilebot" not in p.deployments
        assert "deile-shell" not in p.deployments

    def test_full_deployments(self):
        p = deploy.DeploymentProfile("full")
        assert set(p.deployments) == {
            "deilebot", "deile-worker", "deile-shell", "deile-pipeline"
        }

    def test_claude_only_deployments(self):
        p = deploy.DeploymentProfile("claude-only")
        assert "claude-worker" in p.deployments
        assert "deile-pipeline" in p.deployments
        assert "deile-worker" in p.deployments
        assert "deilebot" not in p.deployments

    def test_requires_discord_only_for_full(self):
        assert deploy.DeploymentProfile("full").requires_discord is True
        assert deploy.DeploymentProfile("pipeline-only").requires_discord is False
        assert deploy.DeploymentProfile("claude-only").requires_discord is False

    def test_includes_bot_only_for_full(self):
        assert deploy.DeploymentProfile("full").includes_bot is True
        assert deploy.DeploymentProfile("pipeline-only").includes_bot is False
        assert deploy.DeploymentProfile("claude-only").includes_bot is False

    def test_includes_claude_worker_only_for_claude_only(self):
        assert deploy.DeploymentProfile("claude-only").includes_claude_worker is True
        assert deploy.DeploymentProfile("full").includes_claude_worker is False
        assert deploy.DeploymentProfile("pipeline-only").includes_claude_worker is False

    def test_pipeline_only_manifests_no_bot_manifests(self):
        m = deploy.DeploymentProfile("pipeline-only").manifests
        assert "20-bot-deployment.yaml" not in m
        assert "19-bot-data-pvc.yaml" not in m
        assert "35-deile-interactive.yaml" not in m
        assert "45-deile-worker-deployment.yaml" in m
        assert "46-deile-pipeline-deployment.yaml" in m

    def test_full_manifests_includes_bot_manifests(self):
        m = deploy.DeploymentProfile("full").manifests
        assert "20-bot-deployment.yaml" in m
        assert "19-bot-data-pvc.yaml" in m
        assert "35-deile-interactive.yaml" in m

    def test_claude_only_manifests_includes_claude_worker_manifests(self):
        m = deploy.DeploymentProfile("claude-only").manifests
        assert "50-claude-worker-deployment.yaml" in m
        assert "48-claude-worker-bearer-secret.yaml" in m
        assert "49-claude-worker-pvc.yaml" in m
        assert "20-bot-deployment.yaml" not in m

    def test_default_profile_is_full(self):
        p = deploy.DeploymentProfile()
        assert p.name == "full"


# ============================================================================
# _persist_env_key
# ============================================================================

class TestPersistEnvKey:
    def test_updates_existing_key(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("DEILE_BOT_AUTH_TOKEN=old-value\nOTHER=x\n")
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        deploy._persist_env_key("DEILE_BOT_AUTH_TOKEN", "new-value")
        content = env_file.read_text()
        assert "DEILE_BOT_AUTH_TOKEN=new-value" in content
        assert "old-value" not in content
        assert "OTHER=x" in content

    def test_appends_new_key_when_absent(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=x\n")
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        deploy._persist_env_key("NEW_KEY", "abc")
        content = env_file.read_text()
        assert "NEW_KEY=abc" in content
        assert "OTHER=x" in content

    def test_noop_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(deploy, "ENV_FILE", tmp_path / "no.env")
        # Must not raise
        deploy._persist_env_key("KEY", "val")

    def test_adds_newline_before_appended_key_if_missing(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("A=1")  # no trailing newline
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        deploy._persist_env_key("B", "2")
        content = env_file.read_text()
        assert "A=1\nB=2" in content

    def test_updates_exported_key(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("export DEILE_BOT_AUTH_TOKEN=old\n")
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        deploy._persist_env_key("DEILE_BOT_AUTH_TOKEN", "new")
        content = env_file.read_text()
        assert "new" in content
        assert "old" not in content


# ============================================================================
# _ensure_persisted_token
# ============================================================================

class TestEnsurePersistedToken:
    def test_returns_existing_value(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("DEILE_BOT_AUTH_TOKEN=existing-token\n")
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        env = {"DEILE_BOT_AUTH_TOKEN": "existing-token"}
        result = deploy._ensure_persisted_token("DEILE_BOT_AUTH_TOKEN", env)
        assert result == "existing-token"
        # File should NOT be modified
        assert "existing-token" in env_file.read_text()

    def test_generates_and_persists_when_absent(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=x\n")
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        env: dict = {}
        result = deploy._ensure_persisted_token("NEW_TOKEN_KEY", env)
        assert len(result) > 16  # token_urlsafe(32) is ~43 chars
        assert env_file.read_text().count("NEW_TOKEN_KEY=") == 1
        assert env["NEW_TOKEN_KEY"] == result

    def test_two_calls_same_token(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=x\n")
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        env: dict = {}
        t1 = deploy._ensure_persisted_token("MY_KEY", env)
        # Simulate second call (env now populated from first call)
        t2 = deploy._ensure_persisted_token("MY_KEY", env)
        assert t1 == t2


# ============================================================================
# _k8s_up_resolve_profile
# ============================================================================

class TestK8sUpResolveProfile:
    def test_cli_flag_takes_precedence(self):
        env = {"DEILE_K8S_DEPLOY_PROFILE": "full"}
        p = deploy._k8s_up_resolve_profile(["--profile", "pipeline-only"], env)
        assert p.name == "pipeline-only"

    def test_env_dict_used_when_no_flag(self):
        p = deploy._k8s_up_resolve_profile([], {"DEILE_K8S_DEPLOY_PROFILE": "claude-only"})
        assert p.name == "claude-only"

    def test_os_environ_fallback(self, monkeypatch):
        monkeypatch.setenv("DEILE_K8S_DEPLOY_PROFILE", "pipeline-only")
        p = deploy._k8s_up_resolve_profile([], {})
        assert p.name == "pipeline-only"

    def test_default_is_full(self, monkeypatch):
        monkeypatch.delenv("DEILE_K8S_DEPLOY_PROFILE", raising=False)
        p = deploy._k8s_up_resolve_profile([], {})
        assert p.name == "full"

    def test_invalid_cli_flag_falls_back_to_full(self, capsys):
        p = deploy._k8s_up_resolve_profile(["--profile", "bogus"], {})
        assert p.name == "full"

    def test_invalid_env_falls_back_to_full(self, capsys):
        p = deploy._k8s_up_resolve_profile([], {"DEILE_K8S_DEPLOY_PROFILE": "bogus"})
        assert p.name == "full"


# ============================================================================
# k8s_up integration (all subprocess mocked)
# ============================================================================

def _make_fake_apply_secret():
    applied = []

    def fake(kubectl, name, kv, ns=""):
        applied.append((name, dict(kv)))
        return True

    return applied, fake


def _make_fake_run(rc=0):
    calls = []

    def fake(cmd, **kw):
        calls.append(list(cmd))
        return rc

    return calls, fake


class TestK8sUpPipelineOnlyProfile:
    """pipeline-only profile must not require DEILE_BOT_DISCORD_TOKEN."""

    def test_succeeds_without_discord_token(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-test\n"
            "DEILE_BOT_AUTH_TOKEN=tok-bearer\n"
            "DEILE_WORKER_BEARER_TOKEN=tok-worker\n"
            "DEILE_PIPELINE_STATUS_TOKEN=tok-ps\n"
        )
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        applied, fake_secret = _make_fake_apply_secret()
        monkeypatch.setattr(deploy, "_apply_secret", fake_secret)
        calls, fake_run = _make_fake_run()
        monkeypatch.setattr(deploy, "_run", fake_run)
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        rc = deploy.k8s_up({
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "pipeline-only"],
        })
        assert rc == 0
        secret_names = [name for name, _ in applied]
        assert "bot-secrets" not in secret_names

    def test_no_bot_manifests_applied(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-test\n"
            "DEILE_BOT_AUTH_TOKEN=tok-bearer\n"
            "DEILE_WORKER_BEARER_TOKEN=tok-worker\n"
            "DEILE_PIPELINE_STATUS_TOKEN=tok-ps\n"
        )
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        monkeypatch.setattr(deploy, "_apply_secret", lambda *a, **kw: True)
        applied_manifests = []

        def fake_run(cmd, **kw):
            if "apply" in cmd and "-f" in cmd:
                applied_manifests.append(Path(cmd[-1]).name)
            return 0

        monkeypatch.setattr(deploy, "_run", fake_run)
        # Only create manifest files that pipeline-only needs
        for name in ("15-bot-config.yaml", "47-deile-runtime-config.yaml",
                     "41-worker-pvc.yaml", "45-deile-worker-deployment.yaml",
                     "46-deile-pipeline-deployment.yaml"):
            (tmp_path / name).write_text("---")
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        deploy.k8s_up({
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "pipeline-only"],
        })
        assert "20-bot-deployment.yaml" not in applied_manifests
        assert "19-bot-data-pvc.yaml" not in applied_manifests
        assert "35-deile-interactive.yaml" not in applied_manifests
        assert "45-deile-worker-deployment.yaml" in applied_manifests


class TestK8sUpFullProfileRequiresDiscord:
    def test_full_profile_fails_without_discord(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        monkeypatch.setattr(deploy, "_apply_secret", lambda *a, **kw: True)
        monkeypatch.setattr(deploy, "_run", lambda *a, **kw: 0)
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        rc = deploy.k8s_up({
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "full"],
        })
        assert rc != 0

    def test_full_profile_succeeds_with_discord(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-test\n"
            "DEILE_BOT_DISCORD_TOKEN=abc.def.ghi\n"
            "DEILE_BOT_AUTH_TOKEN=tok-bearer\n"
            "DEILE_WORKER_BEARER_TOKEN=tok-worker\n"
            "DEILE_PIPELINE_STATUS_TOKEN=tok-ps\n"
        )
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        monkeypatch.setattr(deploy, "_apply_secret", lambda *a, **kw: True)
        monkeypatch.setattr(deploy, "_run", lambda *a, **kw: 0)
        for name in deploy.DeploymentProfile("full").manifests:
            (tmp_path / name).write_text("---")
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        rc = deploy.k8s_up({
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "full"],
        })
        assert rc == 0


class TestK8sUpGitLabToken:
    def test_gitlab_token_in_deile_secrets(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-test\n"
            "GITLAB_TOKEN=glpat-secret\n"
            "DEILE_BOT_AUTH_TOKEN=tok\n"
            "DEILE_WORKER_BEARER_TOKEN=tok-w\n"
            "DEILE_PIPELINE_STATUS_TOKEN=tok-ps\n"
        )
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        applied, fake_secret = _make_fake_apply_secret()
        monkeypatch.setattr(deploy, "_apply_secret", fake_secret)
        monkeypatch.setattr(deploy, "_run", lambda *a, **kw: 0)
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        deploy.k8s_up({
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "pipeline-only"],
        })
        deile_secrets = next(kv for name, kv in applied if name == "deile-secrets")
        assert deile_secrets.get("GITLAB_TOKEN") == "glpat-secret"

    def test_gl_token_alias_used_when_gitlab_token_absent(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-test\n"
            "GL_TOKEN=glpat-alias\n"
            "DEILE_BOT_AUTH_TOKEN=tok\n"
            "DEILE_WORKER_BEARER_TOKEN=tok-w\n"
            "DEILE_PIPELINE_STATUS_TOKEN=tok-ps\n"
        )
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        applied, fake_secret = _make_fake_apply_secret()
        monkeypatch.setattr(deploy, "_apply_secret", fake_secret)
        monkeypatch.setattr(deploy, "_run", lambda *a, **kw: 0)
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        deploy.k8s_up({
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "pipeline-only"],
        })
        deile_secrets = next(kv for name, kv in applied if name == "deile-secrets")
        assert deile_secrets.get("GITLAB_TOKEN") == "glpat-alias"


class TestK8sUpPipelineStatusBearer:
    def test_pipeline_status_bearer_always_created(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-test\n"
            "DEILE_BOT_AUTH_TOKEN=tok\n"
            "DEILE_WORKER_BEARER_TOKEN=tok-w\n"
            "DEILE_PIPELINE_STATUS_TOKEN=tok-ps\n"
        )
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        applied, fake_secret = _make_fake_apply_secret()
        monkeypatch.setattr(deploy, "_apply_secret", fake_secret)
        monkeypatch.setattr(deploy, "_run", lambda *a, **kw: 0)
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        deploy.k8s_up({
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "pipeline-only"],
        })
        secret_names = [name for name, _ in applied]
        assert "pipeline-status-bearer" in secret_names

    def test_pipeline_status_bearer_has_auth_token_key(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-test\n"
            "DEILE_BOT_AUTH_TOKEN=tok\n"
            "DEILE_WORKER_BEARER_TOKEN=tok-w\n"
            "DEILE_PIPELINE_STATUS_TOKEN=my-status-tok\n"
        )
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        applied, fake_secret = _make_fake_apply_secret()
        monkeypatch.setattr(deploy, "_apply_secret", fake_secret)
        monkeypatch.setattr(deploy, "_run", lambda *a, **kw: 0)
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        deploy.k8s_up({
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "pipeline-only"],
        })
        ps_secret = next(kv for name, kv in applied if name == "pipeline-status-bearer")
        assert ps_secret.get("AUTH_TOKEN") == "my-status-tok"


class TestK8sUpTokenPersistence:
    def test_auto_generated_tokens_written_to_env(self, tmp_path, monkeypatch):
        """When tokens are absent from .env, they are generated and persisted."""
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        monkeypatch.setattr(deploy, "_apply_secret", lambda *a, **kw: True)
        monkeypatch.setattr(deploy, "_run", lambda *a, **kw: 0)
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        deploy.k8s_up({
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "pipeline-only"],
        })
        content = env_file.read_text()
        assert "DEILE_BOT_AUTH_TOKEN=" in content
        assert "DEILE_WORKER_BEARER_TOKEN=" in content
        assert "DEILE_PIPELINE_STATUS_TOKEN=" in content

    def test_consecutive_up_uses_same_tokens(self, tmp_path, monkeypatch):
        """Two consecutive k8s up runs must produce the same tokens."""
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
        monkeypatch.setattr(deploy, "ENV_FILE", env_file)
        monkeypatch.setattr(deploy, "ensure_container_prereqs", lambda _: True)
        monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")
        applied_runs = []

        def capturing_secret(kubectl, name, kv, ns=""):
            applied_runs.append((name, dict(kv)))
            return True

        monkeypatch.setattr(deploy, "_apply_secret", capturing_secret)
        monkeypatch.setattr(deploy, "_run", lambda *a, **kw: 0)
        monkeypatch.setattr(deploy, "MANIFESTS", tmp_path)
        monkeypatch.setattr(deploy, "announce_plan", lambda *a, **kw: True)

        args = {
            "yes": True, "dry_run": False,
            "k8s_namespace": None, "extra": ["--profile", "pipeline-only"],
        }
        deploy.k8s_up(args)
        deploy.k8s_up(args)

        # Extract worker-bearer tokens from both runs
        run1_worker = next(
            kv["AUTH_TOKEN"] for name, kv in applied_runs[:3]
            if name == "worker-bearer"
        )
        run2_worker = next(
            kv["AUTH_TOKEN"] for name, kv in applied_runs[3:]
            if name == "worker-bearer"
        )
        assert run1_worker == run2_worker
