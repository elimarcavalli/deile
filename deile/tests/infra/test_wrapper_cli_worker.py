"""Unit tests para ``wrapper.py`` no mode ``cli-worker`` (frota multi-CLI).

O ``cli-worker`` é o entrypoint dos workers da frota (opencode/aider/goose/...).
Espelha o ``claude-worker`` SEM o OAuth do claude: precisa, ANTES de subir o
``cli_worker_server``, carregar a allowlist de repos, ler o bearer e wirar o
``GITHUB_TOKEN``/``GITLAB_TOKEN`` + **identidade git global**. Sem esse setup, o
ciclo de repo do server (clone/commit/push) falharia sem credencial nem
identidade.

Cobre:
  1. ``main(["cli-worker"])`` roteia para ``_run_cli_worker``.
  2. ``_run_cli_worker`` carrega allowlist + bearer + forge creds e delega ao
     ``cli_worker_server.main`` (sem tocar host real — helpers mockados).
  3. Falha hard (rc 78) quando o bearer não está montado.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def wrapper_mod():
    repo_root = Path(__file__).resolve().parents[3]
    wrapper_path = repo_root / "infra" / "k8s" / "wrapper.py"
    spec = importlib.util.spec_from_file_location(
        "wrapper_under_test_cli_worker",
        str(wrapper_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wrapper_under_test_cli_worker"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_main_routes_cli_worker(wrapper_mod, monkeypatch):
    called = {}

    def _fake(rest):
        called["rest"] = rest
        return 0

    monkeypatch.setattr(wrapper_mod, "_run_cli_worker", _fake)
    rc = wrapper_mod.main(["wrapper.py", "cli-worker", "--foo"])
    assert rc == 0
    assert called["rest"] == ["--foo"]


def test_cli_worker_setup_wires_git_and_delegates(
    wrapper_mod,
    tmp_path,
    monkeypatch,
):
    """O setup do cli-worker carrega allowlist + bearer + forge creds e delega.

    Provamos que as etapas críticas (allowlist, identidade git via
    ``_setup_forge_credentials``, leitura do bearer) acontecem ANTES de chamar o
    ``cli_worker_server.main`` — exatamente o gap dos findings 1/2.
    """
    calls = []
    monkeypatch.setattr(
        wrapper_mod, "_harden_runtime_dirs", lambda: calls.append("harden")
    )
    monkeypatch.setattr(
        wrapper_mod,
        "_load_allowed_repo_patterns",
        lambda: (calls.append("allowlist") or ["pat"]),
    )
    monkeypatch.setattr(
        wrapper_mod,
        "_install_git_repo_guard",
        lambda pats: calls.append("guard"),
    )
    monkeypatch.setattr(
        wrapper_mod,
        "_load_secret_files",
        lambda d: (calls.append("secrets") or []),
    )
    monkeypatch.setattr(
        wrapper_mod,
        "_setup_forge_credentials",
        lambda: calls.append("forge_creds"),
    )

    # Bearer montado (lido do arquivo).
    bearer = tmp_path / "CLI_WORKER_BEARER_TOKEN"
    bearer.write_text("tok-123")
    monkeypatch.setattr(
        wrapper_mod.Path,
        "is_file",
        lambda self: str(self).endswith("CLI_WORKER_BEARER_TOKEN"),
    )
    monkeypatch.setattr(
        wrapper_mod.Path,
        "read_text",
        lambda self, *a, **k: "tok-123",
    )

    # Stub do cli_worker_server.main para não subir servidor real.
    import types

    fake_server = types.ModuleType("cli_worker_server")
    fake_server.main = lambda *a, **k: (calls.append("server_main") or 0)
    monkeypatch.setitem(sys.modules, "cli_worker_server", fake_server)

    rc = wrapper_mod._run_cli_worker([])
    assert rc == 0
    # A ordem importa: allowlist e forge creds ANTES do server.
    assert calls.index("forge_creds") < calls.index("server_main")
    assert "allowlist" in calls and "guard" in calls
    assert "secrets" in calls
    # O bearer foi exportado para o env que o server lê.
    import os

    assert os.environ.get("DEILE_CLI_WORKER_AUTH_TOKEN") == "tok-123"


def test_cli_worker_fails_without_bearer(wrapper_mod, monkeypatch):
    """Sem bearer montado → rc 78 (config error), não sobe o server."""
    monkeypatch.setattr(wrapper_mod, "_harden_runtime_dirs", lambda: None)
    monkeypatch.setattr(wrapper_mod, "_load_allowed_repo_patterns", lambda: ["pat"])
    monkeypatch.setattr(wrapper_mod, "_install_git_repo_guard", lambda pats: None)
    # Nenhum arquivo bearer existe.
    monkeypatch.setattr(wrapper_mod.Path, "is_file", lambda self: False)

    rc = wrapper_mod._run_cli_worker([])
    assert rc == 78
