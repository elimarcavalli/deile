"""Integration tests para ``claude_worker_server`` (issue #309 fase 2 Task 12).

O ``claude_worker_server`` é o servidor HTTP do papel ``claude-worker`` no
cluster: recebe dispatches do ``deile-pipeline``, executa ``claude -p`` em
subprocess e devolve resultados. Esta task entrega apenas o esqueleto + o
endpoint ``/v1/health`` — ``/v1/dispatch`` e ``/v1/progress`` ficam como
``501 Not Implemented`` para serem preenchidos nas Tasks 13 e 14.

O módulo vive em ``infra/k8s/``, não no pacote Python. Carregamos via
``importlib.util`` (mesmo padrão de ``test_wrapper_claude_worker.py``) para
manter os testes isolados — sem mexer em ``sys.path`` global.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def claude_worker_module():
    """Carrega ``infra/k8s/claude_worker_server.py`` dinamicamente.

    Cada teste recebe uma instância nova do módulo, evitando contaminação
    cross-teste (caches, handlers já registrados, etc.).
    """
    repo_root = Path(__file__).resolve().parents[3]
    server_path = repo_root / "infra" / "k8s" / "claude_worker_server.py"
    spec = importlib.util.spec_from_file_location(
        "claude_worker_server_under_test", str(server_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_worker_server_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_health_returns_200_when_binary_present(
    claude_worker_module, monkeypatch,
):
    """``/v1/health`` retorna 200 + caminho do binário quando ``claude``
    está no ``PATH``."""
    monkeypatch.setattr(
        claude_worker_module.shutil,
        "which",
        lambda b: "/usr/local/bin/claude" if b == "claude" else None,
    )

    app = claude_worker_module.build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["claude_binary"] == "/usr/local/bin/claude"


async def test_health_returns_500_when_binary_missing(
    claude_worker_module, monkeypatch,
):
    """``/v1/health`` retorna 500 quando o binário ``claude`` não está no
    ``PATH`` — o pod é removido do Service pelo readinessProbe."""
    monkeypatch.setattr(claude_worker_module.shutil, "which", lambda b: None)

    app = claude_worker_module.build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 500
        body = await resp.json()
        # A mensagem precisa mencionar o binário/PATH para diagnóstico do
        # operador — não exigimos string exata para permitir refino futuro.
        assert "claude" in body["error"].lower()


async def test_dispatch_returns_501_stub(claude_worker_module):
    """``POST /v1/dispatch`` é stub na Task 12; Task 13 implementa o spawn
    do ``claude -p`` e a serialização da resposta."""
    app = claude_worker_module.build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/dispatch", json={"brief": "x", "channel_id": "y"},
        )
        assert resp.status == 501


async def test_progress_returns_501_stub(claude_worker_module):
    """``GET /v1/progress/{task_id}`` é stub na Task 12; Task 14 implementa
    o tail dos arquivos de stdout/stderr persistidos no PVC."""
    app = claude_worker_module.build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/abc12345")
        assert resp.status == 501
