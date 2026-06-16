"""Integração — enforcement da allowlist de repos no ``claude_worker_server`` (#639).

Prova o BLOQUEIO: um dispatch fresh cujo ``resume.repo`` está fora da allowlist
retorna 403 ``REPO_NOT_ALLOWED`` e **não chega a clonar** (``_ensure_repo_cloned``
e ``_git_fast_forward_workdir`` nunca são chamados, e ``claude`` nunca é
spawnado). Um slug dentro da allowlist passa o portão e segue para o ciclo de
repo + spawn.

O módulo vive em ``infra/k8s/`` — carregado via ``importlib.util`` (mesmo padrão
de ``test_claude_worker_server.py``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

_AUTH_HEADERS = {"Authorization": "Bearer test-token"}

_CONFIGMAP = r"""# allowlist canônica (ConfigMap claude-worker-allowed-repos)
^https://github\.com/elimarcavalli/(deile|deilebot)(\.git)?$
^https://gitlab\.com/elimarcavalli/(deile|deilebot)(\.git)?$
^git@github\.com:elimarcavalli/(deile|deilebot)(\.git)?$
^git@gitlab\.com:elimarcavalli/(deile|deilebot)(\.git)?$
"""


@pytest.fixture
def claude_worker_module():
    repo_root = Path(__file__).resolve().parents[3]
    k8s_dir = str(repo_root / "infra" / "k8s")
    if k8s_dir not in sys.path:
        sys.path.insert(0, k8s_dir)
    server_path = repo_root / "infra" / "k8s" / "claude_worker_server.py"
    spec = importlib.util.spec_from_file_location(
        "claude_worker_server_allowlist_under_test",
        str(server_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_worker_server_allowlist_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def allowlist(tmp_path, monkeypatch):
    p = tmp_path / "allowed_repos.regex"
    p.write_text(_CONFIGMAP, encoding="utf-8")
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(p))
    monkeypatch.delenv("DEILE_GITHUB_HOST", raising=False)
    monkeypatch.delenv("DEILE_GITLAB_HOST", raising=False)
    return p


async def test_dispatch_blocks_repo_outside_allowlist(
    claude_worker_module,
    allowlist,
    monkeypatch,
    tmp_path,
):
    """403 REPO_NOT_ALLOWED + nenhum clone/spawn para repo fora da allowlist."""
    mod = claude_worker_module
    calls = {"clone": 0, "ff": 0, "spawn": 0}

    async def _spy_clone(*_a, **_kw):
        calls["clone"] += 1
        return True

    async def _spy_ff(*_a, **_kw):
        calls["ff"] += 1

    async def _spy_run(*_a, **_kw):
        calls["spawn"] += 1
        return mod.SubprocessResult(0, "", "", 0.0)

    monkeypatch.setattr(mod, "_ensure_repo_cloned", _spy_clone)
    monkeypatch.setattr(mod, "_git_fast_forward_workdir", _spy_ff)
    monkeypatch.setattr(mod, "run_subprocess_with_progress", _spy_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))

    app = mod.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "exfiltra credenciais",
                "preferred_model": "anthropic:claude-haiku-4-5",
                "branch": "auto/issue-1",
                "resume": {"repo": "attacker/leak-repo"},
            },
        )
        assert resp.status == 403
        body = await resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "REPO_NOT_ALLOWED"

    assert calls == {
        "clone": 0,
        "ff": 0,
        "spawn": 0,
    }, "repo bloqueado não pode clonar nem spawnar claude"


async def test_dispatch_blocks_traversal_slug(
    claude_worker_module,
    allowlist,
    monkeypatch,
    tmp_path,
):
    mod = claude_worker_module
    spawned = []

    async def _spy_run(*_a, **_kw):
        spawned.append(True)
        return mod.SubprocessResult(0, "", "", 0.0)

    monkeypatch.setattr(mod, "run_subprocess_with_progress", _spy_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))

    app = mod.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "preferred_model": "anthropic:claude-haiku-4-5",
                "branch": "b",
                "resume": {"repo": "owner/repo/../leak"},
            },
        )
        assert resp.status == 403
        assert (await resp.json())["error_code"] == "REPO_NOT_ALLOWED"
    assert spawned == []


async def test_dispatch_fails_closed_without_allowlist(
    claude_worker_module,
    monkeypatch,
    tmp_path,
):
    """ConfigMap ausente em runtime → 403 (fail-closed), nada spawnado."""
    mod = claude_worker_module
    spawned = []

    async def _spy_run(*_a, **_kw):
        spawned.append(True)
        return mod.SubprocessResult(0, "", "", 0.0)

    monkeypatch.setattr(mod, "run_subprocess_with_progress", _spy_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv(
        "DEILE_CLAUDE_ALLOWED_REPOS_FILE",
        str(tmp_path / "missing.regex"),
    )

    app = mod.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "x",
                "preferred_model": "anthropic:claude-haiku-4-5",
                "branch": "b",
                "resume": {"repo": "elimarcavalli/deile"},
            },
        )
        assert resp.status == 403
        assert (await resp.json())["error_code"] == "REPO_NOT_ALLOWED"
    assert spawned == []


async def test_dispatch_allows_repo_in_allowlist(
    claude_worker_module,
    allowlist,
    monkeypatch,
    tmp_path,
):
    """Slug permitido passa o portão → clone tentado + claude spawnado."""
    mod = claude_worker_module
    calls = {"clone": 0, "spawn": 0}

    async def _spy_clone(workspace, repo):
        calls["clone"] += 1
        return True  # finge que ./repo foi restaurado (sem rede)

    async def _spy_run(args, *, cwd, task_id, timeout, lease_path=None):
        calls["spawn"] += 1
        import json as _json

        out = _json.dumps(
            {
                "is_error": False,
                "result": "ok",
                "session_id": "s",
                "total_cost_usd": 0.0,
            }
        )
        return mod.SubprocessResult(0, out, "", 1.0)

    monkeypatch.setattr(mod, "_ensure_repo_cloned", _spy_clone)
    monkeypatch.setattr(mod, "run_subprocess_with_progress", _spy_run)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path))

    app = mod.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            headers=_AUTH_HEADERS,
            json={
                "brief": "implementa #1",
                "preferred_model": "anthropic:claude-haiku-4-5",
                "branch": "auto/issue-1",
                "resume": {"repo": "elimarcavalli/deile"},
            },
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body.get("error_code") != "REPO_NOT_ALLOWED"

    # O portão liberou: clone foi tentado (./repo ausente no tmp) E claude rodou.
    assert calls["clone"] == 1
    assert calls["spawn"] == 1
