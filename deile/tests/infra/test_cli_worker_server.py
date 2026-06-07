"""Testes do servidor genérico ``cli_worker_server`` (Fase 2 — framework).

Cobre o server agnóstico de CLI com um **adapter mock** registrado em runtime:
  - ``/v1/health`` reflete kind/auth_mode/ready (ready=False sem auth key).
  - ``/v1/models`` retorna o catálogo do adapter.
  - ``/v1/dispatch`` escreve o brief, roda o argv via core, aplica o gate de
    git (sem commit/push → NO_PUSH; com → ok).
  - ``/v1/progress`` devolve o tail persistido no PVC.
  - Bearer auth: paths protegidos exigem token; ``/v1/health`` é aberto.

O pacote ``cli_adapters`` e o módulo ``cli_worker_server`` vivem em
``infra/k8s/`` — path inserido manualmente (convenção dos testes de infra).
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


@pytest.fixture
def mock_adapter(tmp_path, monkeypatch):
    """Registra um adapter mock 'mock' e seleciona-o via env.

    O adapter escreve um arquivo no workdir e (opcionalmente) faz commit/push
    simulado controlado pelo monkeypatch dos helpers de git do server.
    """
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / "zzz_mock_worker.py"
    mod_path.write_text(textwrap.dedent('''\
        from cli_adapters.base import BaseCliAdapter, WorkResult, ModelInfo


        class MockAdapter(BaseCliAdapter):
            def build_argv(self, *, brief_path, model, reasoning, workdir, resume):
                # Comando trivial: cria um marcador no workdir e imprime OK.
                return ["sh", "-c",
                        f"echo dispatched model={model}; touch {workdir}/.ran"]

            def parse_output(self, *, stdout, stderr, rc):
                return WorkResult(ok=(rc == 0), result_text=stdout.strip()[:80])

            def list_models(self):
                return [
                    ModelInfo(id="openrouter/deepseek/deepseek-chat",
                              provider="openrouter", context=64000),
                ]


        ADAPTER = MockAdapter(
            kind="mock", default_port=8799, auth_env_keys=["MOCK_API_KEY"],
        )
    '''), encoding="utf-8")
    cli_adapters.reload_adapters()

    monkeypatch.setenv("DEILE_CLI_WORKER_KIND", "mock")
    monkeypatch.setenv("DEILE_CLI_WORKER_ROOT", str(tmp_path / "work"))
    # Disable real lease TTL waits: keep defaults (fast).
    try:
        yield
    finally:
        mod_path.unlink(missing_ok=True)
        sys.modules.pop("cli_adapters.zzz_mock_worker", None)
        cli_adapters.reload_adapters()
        cws._models_cache.clear()


async def test_health_not_ready_without_auth_key(mock_adapter, monkeypatch):
    monkeypatch.delenv("MOCK_API_KEY", raising=False)
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 503
        body = await resp.json()
        assert body["kind"] == "mock"
        assert body["auth_mode"] == "env"
        assert body["ready"] is False


async def test_health_ready_with_auth_key(mock_adapter, monkeypatch):
    monkeypatch.setenv("MOCK_API_KEY", "secret")
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 200
        assert (await resp.json())["ready"] is True


async def test_health_is_open_no_bearer_required(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")  # sem header
        assert resp.status in (200, 503)  # não 401


async def test_models_requires_bearer(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/models")  # sem header
        assert resp.status == 401


async def test_models_returns_adapter_catalog(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/models", headers=_AUTH_HEADERS)
        assert resp.status == 200
        body = await resp.json()
        assert body["kind"] == "mock"
        ids = [m["id"] for m in body["models"]]
        assert "openrouter/deepseek/deepseek-chat" in ids


async def test_dispatch_missing_brief_returns_400(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch", json={"stage": "implement"}, headers=_AUTH_HEADERS,
        )
        assert resp.status == 400


async def test_dispatch_gate_fails_without_push(mock_adapter, monkeypatch):
    """Adapter retorna ok mas não há commit/push → gate reprova com NO_PUSH."""
    # Sem repo git no workdir → _git_head None, _git_branch_pushed False.
    monkeypatch.setattr(cws, "_git_head", _async_return(None))
    monkeypatch.setattr(cws, "_git_branch_pushed", _async_return(False))

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={"brief": "do the thing", "stage": "implement",
                  "branch": "auto/issue-1", "cli_model": "x"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is False
        assert body["error_code"] == "NO_PUSH"
        assert body["task_id"]


async def test_dispatch_success_with_commit_and_push(mock_adapter, monkeypatch):
    """Adapter ok + commit novo + push confirmado → ok=True."""
    seq = {"head": ["base-sha", "new-sha"]}

    async def _fake_head(_workdir):
        # 1ª chamada (base) = base-sha; 2ª (pós-run) = new-sha.
        return seq["head"].pop(0) if seq["head"] else "new-sha"

    async def _fake_pushed(_workdir, _branch):
        return True

    monkeypatch.setattr(cws, "_git_head", _fake_head)
    monkeypatch.setattr(cws, "_git_branch_pushed", _fake_pushed)

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={"brief": "do the thing", "stage": "implement",
                  "branch": "auto/issue-1", "cli_model": "x"},
            headers=_AUTH_HEADERS,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert body["returncode"] == 0
        assert "dispatched model=x" in body["result"]


async def test_dispatch_writes_brief_file(mock_adapter, monkeypatch, tmp_path):
    """O brief é gravado em <workdir>/.brief.md antes do build_argv."""
    monkeypatch.setattr(cws, "_git_head", _async_return("h"))
    monkeypatch.setattr(cws, "_git_branch_pushed", _async_return(True))

    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch",
            json={"brief": "BRIEF-CONTENT-MARKER", "branch": "b"},
            headers=_AUTH_HEADERS,
        )
        body = await resp.json()
    task_id = body["task_id"]
    brief = tmp_path / "work" / task_id / ".brief.md"
    assert brief.is_file()
    assert brief.read_text() == "BRIEF-CONTENT-MARKER"


async def test_progress_404_for_unknown_task(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/progress/" + "a" * 16, headers=_AUTH_HEADERS,
        )
        assert resp.status == 404


async def test_progress_400_for_invalid_task_id(mock_adapter):
    app = cws.build_app(auth_token="test-token")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/progress/../etc", headers=_AUTH_HEADERS,
        )
        assert resp.status in (400, 404)  # traversal barrado


def test_resolve_adapter_requires_kind(monkeypatch):
    monkeypatch.delenv("DEILE_CLI_WORKER_KIND", raising=False)
    with pytest.raises(RuntimeError):
        cws._resolve_adapter()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _async_return(value):
    """Factory de coroutine que ignora args e retorna *value*."""
    async def _coro(*_a, **_kw):
        return value
    return _coro
