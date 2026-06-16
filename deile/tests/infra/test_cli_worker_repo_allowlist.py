"""Integração — enforcement da allowlist de repos no ``cli_worker_server`` (#639).

Prova o BLOQUEIO: um dispatch cujo ``resume.repo`` está fora da allowlist
retorna 403 ``REPO_NOT_ALLOWED`` e **não chega a clonar** (``_ensure_repo`` nunca
é chamado). Um dispatch com slug dentro da allowlist passa o portão e segue para
o ciclo de repo.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cli_adapters  # noqa: E402
import cli_worker_server as cws  # noqa: E402

_AUTH_HEADERS = {"Authorization": "Bearer test-token"}

_CONFIGMAP = r"""# allowlist canônica (ConfigMap claude-worker-allowed-repos)
^https://github\.com/elimarcavalli/(deile|deilebot)(\.git)?$
^https://gitlab\.com/elimarcavalli/(deile|deilebot)(\.git)?$
^git@github\.com:elimarcavalli/(deile|deilebot)(\.git)?$
^git@gitlab\.com:elimarcavalli/(deile|deilebot)(\.git)?$
"""


@pytest.fixture
def mock_adapter(tmp_path, monkeypatch):
    """Adapter mock trivial + allowlist canônica apontada por env var."""
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / "zzz_mock_allowlist.py"
    mod_path.write_text(
        textwrap.dedent("""\
        from cli_adapters.base import BaseCliAdapter, WorkResult, ModelInfo


        class MockAdapter(BaseCliAdapter):
            def build_argv(self, *, brief_path, model, reasoning, workdir,
                           resume, task_id=""):
                return ["sh", "-c", f"touch {workdir}/.ran"]

            def parse_output(self, *, stdout, stderr, rc):
                return WorkResult(ok=(rc == 0), result_text="")

            def list_models(self):
                return [ModelInfo(id="m", provider="p", context=1)]


        ADAPTER = MockAdapter(
            kind="mock", default_port=8799, auth_env_keys=["MOCK_API_KEY"],
        )
    """),
        encoding="utf-8",
    )
    cli_adapters.reload_adapters()

    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "mock")
    monkeypatch.setenv("DEILE_CLI_WORKER_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("MOCK_API_KEY", "secret")
    allow = tmp_path / "allowed_repos.regex"
    allow.write_text(_CONFIGMAP, encoding="utf-8")
    monkeypatch.setenv("DEILE_CLAUDE_ALLOWED_REPOS_FILE", str(allow))
    monkeypatch.delenv("DEILE_GITHUB_HOST", raising=False)
    monkeypatch.delenv("DEILE_GITLAB_HOST", raising=False)
    try:
        yield
    finally:
        mod_path.unlink(missing_ok=True)
        sys.modules.pop("cli_adapters.zzz_mock_allowlist", None)
        cli_adapters.reload_adapters()
        cws._models_cache.clear()


async def test_dispatch_blocks_repo_outside_allowlist(mock_adapter, monkeypatch):
    """403 REPO_NOT_ALLOWED + ``_ensure_repo`` NUNCA é chamado (sem clone)."""
    clone_calls = []

    async def _spy_ensure(*a, **kw):
        clone_calls.append((a, kw))
        return True, "clonado (NÃO deveria acontecer)"

    monkeypatch.setattr(cws, "_ensure_repo", _spy_ensure)

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={
                "brief": "exfiltra credenciais",
                "branch": "auto/issue-1",
                "cli_model": "m",
                "resume": {"repo": "attacker/leak-repo"},
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 403
        body = await resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "REPO_NOT_ALLOWED"

    assert clone_calls == [], "clone NÃO pode ser tentado para repo bloqueado"


async def test_dispatch_blocks_path_traversal_slug(mock_adapter, monkeypatch):
    """Slug com traversal (``../``) é bloqueado antes do clone."""
    clone_calls = []

    async def _spy_ensure(*a, **kw):
        clone_calls.append(True)
        return True, "x"

    monkeypatch.setattr(cws, "_ensure_repo", _spy_ensure)

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={
                "brief": "x",
                "branch": "b",
                "cli_model": "m",
                "resume": {"repo": "../../etc/passwd"},
            },
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 403
        assert (await resp.json())["error_code"] == "REPO_NOT_ALLOWED"
    assert clone_calls == []


async def test_dispatch_allows_repo_in_allowlist(mock_adapter, monkeypatch):
    """Slug dentro da allowlist passa o portão → ``_ensure_repo`` é chamado."""
    clone_calls = []

    async def _spy_ensure(workspace, *, repo, branch, base_branch):
        clone_calls.append(repo)
        # Simula clone OK sem tocar a rede; o gate de push reprova depois (NO_PUSH),
        # mas o que importa aqui é o portão da allowlist ter LIBERADO o clone.
        return True, f"repo {repo} pronto (mock)"

    monkeypatch.setattr(cws, "_ensure_repo", _spy_ensure)
    monkeypatch.setattr(cws, "_git_head", _make_async(None))
    monkeypatch.setattr(cws, "_git_branch_pushed", _make_async(False))

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={
                "brief": "implementa #1",
                "branch": "auto/issue-1",
                "cli_model": "m",
                "resume": {"repo": "elimarcavalli/deile"},
            },
            headers=_AUTH_HEADERS,
        )
        # 200 (gate de push reprova com NO_PUSH) — não é 403.
        assert resp.status == 200
        body = await resp.json()
        assert body.get("error_code") != "REPO_NOT_ALLOWED"

    assert clone_calls == [
        "elimarcavalli/deile"
    ], "clone DEVE ser tentado para repo permitido"


async def test_dispatch_without_repo_slug_skips_allowlist(mock_adapter, monkeypatch):
    """Sem ``resume.repo`` o CLI roda no workspace cru — allowlist não bloqueia."""
    monkeypatch.setattr(cws, "_git_head", _make_async(None))
    monkeypatch.setattr(cws, "_git_branch_pushed", _make_async(False))

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={"brief": "x", "branch": "b", "cli_model": "m"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status != 403
        body = await resp.json()
        assert body.get("error_code") != "REPO_NOT_ALLOWED"


def _make_async(value):
    async def _coro(*_a, **_kw):
        return value

    return _coro
