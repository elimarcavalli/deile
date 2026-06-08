"""Ciclo de repo REAL do cli_worker_server (findings 1/2/6).

Exercita, contra um remote git LOCAL (bare repo, sem rede), o que a revisão
cética apontou como ausente:

  - clone + checkout/criação do branch (``_worker_core.ensure_repo_and_branch``);
  - commit pelo adapter + push REAL para o remote (gate verifica push real);
  - fallback commit do wrapper quando o adapter ``brief_driven`` não commitou
    (``error_code=WRAPPER_COMMITTED``);
  - estratégia ``cli_autocommit`` (aider-like): o adapter commita, o wrapper só
    pusha.

Diferente de ``test_cli_worker_server.py`` (que mocka os helpers de git), aqui
o git roda DE VERDADE — clone, commit, push, ls-remote — provando o fluxo
ponta-a-ponta sem precisar de ``gh``/``glab`` nem rede.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _worker_core as core  # noqa: E402
import cli_adapters  # noqa: E402
import cli_worker_server as cws  # noqa: E402

_AUTH = {"Authorization": "Bearer test-token"}


def _git(*args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def local_remote(tmp_path):
    """Cria um remote git bare local com um commit inicial em ``main``.

    Devolve ``(bare_path, seed_path)``: ``bare_path`` serve de ``origin``;
    ``seed_path`` é um clone de trabalho usado só para semear o histórico.
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "--initial-branch=main", ".", cwd=bare)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "--initial-branch=main", ".", cwd=seed)
    _git("config", "user.email", "t@t.io", cwd=seed)
    _git("config", "user.name", "Tester", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("remote", "add", "origin", str(bare), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)
    return bare, seed


@pytest.fixture
def patched_clone(monkeypatch, local_remote):
    """Faz ``ensure_repo_and_branch`` clonar do remote LOCAL (sem gh/glab/rede).

    Substitui só o passo de clone (``_git_or_gh_clone``) por um ``git clone`` do
    bare local; o resto de ``ensure_repo_and_branch`` (fetch + checkout/criação
    do branch) roda real.
    """
    bare, _seed = local_remote

    async def _fake_clone(_forge_cli, _repo, workspace, _timeout):
        return await core._git("clone", str(bare), "repo", cwd=workspace, timeout=60)

    monkeypatch.setattr(core, "_git_or_gh_clone", _fake_clone)
    return bare


async def test_ensure_repo_and_branch_clones_and_creates_branch(
    tmp_path, patched_clone,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ok, detail = await core.ensure_repo_and_branch(
        workspace, repo="owner/repo", branch="auto/issue-7", base_branch="main",
    )
    assert ok, detail
    repo = workspace / "repo"
    assert (repo / ".git").exists()
    # Está no branch novo, criado a partir de main.
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "auto/issue-7"


@pytest.fixture
def repo_adapter(tmp_path, monkeypatch, patched_clone):
    """Registra um adapter cujo build_argv commita (brief_driven) e pusha.

    O ``git_strategy`` é parametrizado via env do teste (default brief_driven).
    O subprocess roda git real dentro do checkout.
    """
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / "zzz_repo_mock.py"
    mod_path.write_text(textwrap.dedent('''\
        import os
        from cli_adapters.base import BaseCliAdapter, WorkResult, ModelInfo


        class RepoMock(BaseCliAdapter):
            def build_argv(self, *, brief_path, model, reasoning, workdir, resume, task_id=""):
                # Em workdir (= checkout), cria um arquivo. Commit só se o
                # git_strategy do teste pedir (env MOCK_DO_COMMIT=1).
                script = (
                    f"set -e; cd {workdir}; "
                    "echo change > feature.txt; "
                )
                if os.environ.get("MOCK_DO_COMMIT") == "1":
                    script += "git add -A; git commit -m 'feat: adapter commit'; "
                script += "echo DONE"
                return ["sh", "-c", script]

            def parse_output(self, *, stdout, stderr, rc):
                return WorkResult(ok=("DONE" in stdout), result_text=stdout.strip()[:120])

            def list_models(self):
                return [ModelInfo(id="local/model")]


        ADAPTER = RepoMock(kind="zzz_repo_mock", default_port=8797)
    '''), encoding="utf-8")

    cli_adapters.reload_adapters()
    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "zzz_repo_mock")
    monkeypatch.setenv("DEILE_CLI_WORKER_ROOT", str(tmp_path / "work"))
    # A identidade git global existe no ambiente de teste; o push vai para o
    # remote local (path do bare), sem credencial nem rede.
    try:
        yield
    finally:
        mod_path.unlink(missing_ok=True)
        sys.modules.pop("cli_adapters.zzz_repo_mock", None)
        cli_adapters.reload_adapters()
        cws._models_cache.clear()


def _set_adapter_strategy(strategy: str):
    """Ajusta o git_strategy da instância ADAPTER recém-carregada."""
    # A instância é criada pelo auto-discovery; localiza e seta o atributo.
    adapter = cli_adapters.ADAPTERS["zzz_repo_mock"]
    object.__setattr__(adapter, "git_strategy", strategy)
    return adapter


async def test_dispatch_brief_driven_agent_commits_then_pushes(
    repo_adapter, monkeypatch, tmp_path,
):
    """brief_driven + agente commitou → wrapper pusha → gate vê push real → ok."""
    _set_adapter_strategy("brief_driven")
    monkeypatch.setenv("MOCK_DO_COMMIT", "1")

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={
                "brief": "implemente",
                "stage": "implement",
                "branch": "auto/issue-7",
                "cli_model": "local/model",
                "resume": {"repo": "owner/repo", "main_branch": "main"},
            },
            headers=_AUTH,
        )
        assert resp.status == 200
        body = await resp.json()
    # Agente commitou + wrapper pushou → gate vê push real (ls-remote) → ok.
    assert body["ok"] is True, body
    assert body.get("error_code") is None


async def test_dispatch_brief_driven_no_commit_triggers_wrapper_fallback(
    repo_adapter, monkeypatch,
):
    """brief_driven SEM commit do agente → fallback commit (WRAPPER_COMMITTED)."""
    _set_adapter_strategy("brief_driven")
    monkeypatch.setenv("MOCK_DO_COMMIT", "0")  # agente só escreve, não commita

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={
                "brief": "implemente",
                "stage": "implement",
                "branch": "auto/issue-8",
                "cli_model": "local/model",
                "resume": {"repo": "owner/repo", "main_branch": "main"},
            },
            headers=_AUTH,
        )
        assert resp.status == 200
        body = await resp.json()
    # Sucesso degradado: wrapper commitou + pushou → ok=True, code WRAPPER_COMMITTED.
    assert body["ok"] is True, body
    assert body.get("error_code") == "WRAPPER_COMMITTED"


async def test_dispatch_cli_autocommit_only_pushes(repo_adapter, monkeypatch):
    """cli_autocommit (aider-like): adapter commita, wrapper só pusha → ok."""
    _set_adapter_strategy("cli_autocommit")
    monkeypatch.setenv("MOCK_DO_COMMIT", "1")

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={
                "brief": "implemente",
                "stage": "implement",
                "branch": "auto/issue-9",
                "cli_model": "local/model",
                "resume": {"repo": "owner/repo", "main_branch": "main"},
            },
            headers=_AUTH,
        )
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is True, body
    # cli_autocommit não mexe em error_code (sem fallback).
    assert body.get("error_code") is None


async def test_dispatch_repo_setup_failure_returns_typed_error(
    repo_adapter, monkeypatch,
):
    """Clone falha → REPO_SETUP_FAILED (não roda o CLI contra dir sem repo)."""
    async def _fail(*_a, **_k):
        return (False, "clone falhou: remote inacessível")

    monkeypatch.setattr(cws, "_ensure_repo", _fail)
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={
                "brief": "implemente",
                "stage": "implement",
                "branch": "auto/issue-10",
                "cli_model": "local/model",
                "resume": {"repo": "owner/repo", "main_branch": "main"},
            },
            headers=_AUTH,
        )
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is False
    assert body["error_code"] == "REPO_SETUP_FAILED"
