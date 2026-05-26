"""Unit tests para _claude_install.bootstrap_claude_worker (#309 fase 2)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

WORKER_DIR = Path(__file__).resolve().parents[3] / "infra" / "k8s"


@pytest.fixture
def claude_install_module():
    sys.path.insert(0, str(WORKER_DIR))
    try:
        import importlib

        import _claude_install

        importlib.reload(_claude_install)
        yield _claude_install
    finally:
        sys.path.remove(str(WORKER_DIR))


def test_bootstrap_returns_error_when_no_credentials_and_not_interactive(
    claude_install_module, tmp_path, monkeypatch,
):
    """No credentials at host + interactive=False -> fail fast."""
    monkeypatch.setenv("HOME", str(tmp_path))  # ~/.claude/credentials.json ausente

    result = claude_install_module.bootstrap_claude_worker(
        interactive=False, force_relogin=False, home=tmp_path,
    )
    assert result.ok is False
    assert "credentials" in (result.error or "").lower()


def test_bootstrap_idempotent_when_credentials_present(
    claude_install_module, tmp_path, monkeypatch,
):
    """Credentials existing + cluster commands mockados -> ok=True idempotent."""
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(
        json.dumps({"email": "user@test.com", "access_token": "fake_token"})
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    # Mock all kubectl subprocess calls to return 0
    with patch.object(claude_install_module, "_kubectl_apply_secret", return_value=True), \
         patch.object(claude_install_module, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is True
    assert result.account_email == "user@test.com"
    assert result.secret_applied is True
    assert result.deployment_applied is True
    assert result.rollout_ready is True


def test_bootstrap_force_relogin_runs_claude_logout_then_login(
    claude_install_module, tmp_path, monkeypatch,
):
    """force_relogin=True -> claude logout + claude login antes de continuar."""
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(json.dumps({"email": "u@x"}))
    monkeypatch.setenv("HOME", str(tmp_path))

    called_commands = []

    def fake_run(cmd, *args, **kwargs):
        called_commands.append(cmd)
        ret = MagicMock()
        ret.returncode = 0
        return ret

    with patch.object(claude_install_module.subprocess, "run", side_effect=fake_run), \
         patch.object(claude_install_module, "_kubectl_apply_secret", return_value=True), \
         patch.object(claude_install_module, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=True):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=True, force_relogin=True, home=tmp_path,
        )

    assert result.ok is True
    cmd_strs = [" ".join(c) if isinstance(c, list) else str(c) for c in called_commands]
    assert any("logout" in s for s in cmd_strs), \
        f"expected `claude logout` in {cmd_strs}"
    assert any("login" in s for s in cmd_strs), \
        f"expected `claude login` in {cmd_strs}"


def test_bootstrap_failure_in_secret_apply_returns_error(
    claude_install_module, tmp_path, monkeypatch,
):
    """Secret apply falha -> ClaudeLoginResult.ok=False com error explicado."""
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(json.dumps({"email": "u@x"}))
    monkeypatch.setenv("HOME", str(tmp_path))

    with patch.object(claude_install_module, "_kubectl_apply_secret", return_value=False):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is False
    assert "secret" in (result.error or "").lower()


def test_bootstrap_failure_in_rollout_returns_error(
    claude_install_module, tmp_path, monkeypatch,
):
    """Rollout timeout -> ok=False, manifests aplicados mas rollout pendente."""
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text(json.dumps({"email": "u@x"}))
    monkeypatch.setenv("HOME", str(tmp_path))

    with patch.object(claude_install_module, "_kubectl_apply_secret", return_value=True), \
         patch.object(claude_install_module, "_kubectl_apply_manifests", return_value=True), \
         patch.object(claude_install_module, "_kubectl_wait_rollout", return_value=False):
        result = claude_install_module.bootstrap_claude_worker(
            interactive=False, force_relogin=False, home=tmp_path,
        )

    assert result.ok is False
    assert result.deployment_applied is True  # passou aqui
    assert result.rollout_ready is False
    assert "rollout" in (result.error or "").lower()
