"""Tests para o endpoint ``GET /v1/progress/{task_id}`` do worker_server
(issue #257 — snapshot mid-flight de progresso).

Inserimos ``infra/k8s`` em sys.path (mesma técnica de
``test_infra_tooling.py``), montamos o app via ``build_app`` com um token de
teste e exercitamos o endpoint via ``aiohttp.test_utils.TestClient``.

Cobertura mínima:
  * 404 quando task_id é desconhecido.
  * 401 sem ou com Bearer inválido.
  * Shape correto durante a execução (ok=None, phase, progress_lines, elapsed_s).
  * Shape correto após terminal (ok=True, elapsed_s congelado, files).
  * Chave interna ``_mono_start`` é stripada do ``/v1/result/{task_id}``.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

aiohttp_test_utils = pytest.importorskip("aiohttp.test_utils")

import worker_server  # noqa: E402


pytestmark = pytest.mark.unit


_TOKEN = "test-token-0123456789abcdef"


@pytest.fixture
def _clean_tasks():
    """Isola o _TASKS dict entre testes para evitar vazamento de estado."""
    worker_server._TASKS.clear()
    yield
    worker_server._TASKS.clear()


@pytest.fixture
async def client(_clean_tasks):
    """Sobe o app aiohttp num servidor de teste sem TCP real."""
    app = worker_server.build_app(_TOKEN)
    async with aiohttp_test_utils.TestClient(
        aiohttp_test_utils.TestServer(app)
    ) as cli:
        yield cli


async def _get(client, path, token=_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.get(path, headers=headers)


async def test_progress_404_when_task_unknown(client):
    resp = await _get(client, "/v1/progress/does-not-exist")
    assert resp.status == 404
    data = await resp.json()
    assert data["error"]["code"] == "NOT_FOUND"


async def test_progress_401_without_bearer(client):
    resp = await client.get("/v1/progress/anything")
    assert resp.status == 401


async def test_progress_401_with_bad_bearer(client):
    resp = await _get(client, "/v1/progress/anything", token="wrong-token-xxxxxxxxx")
    assert resp.status == 401


async def test_progress_midflight_returns_phase_and_progress_lines(client):
    """Simula uma task em execução escrevendo direto no ``_TASKS`` dict."""
    task_id = "midflight1234"
    worker_server._TASKS[task_id] = {
        "task_id": task_id,
        "ok": None,
        "started_at": "2026-01-01T00:00:00+00:00",
        "brief": "test brief",
        "phase": "▶️  trabalhando...",
        "current_activity": "tool_invoked:bash_execute",
        "progress_lines": [
            "tool_invoked:read_file",
            "tool_completed:read_file",
            "tool_invoked:bash_execute",
        ],
        "_mono_start": time.monotonic() - 1.5,
    }

    resp = await _get(client, f"/v1/progress/{task_id}")
    assert resp.status == 200
    data = await resp.json()
    assert data["task_id"] == task_id
    assert data["ok"] is None  # ainda rodando
    assert data["phase"] == "▶️  trabalhando..."
    assert data["current_activity"] == "tool_invoked:bash_execute"
    assert data["progress_lines"] == [
        "tool_invoked:read_file",
        "tool_completed:read_file",
        "tool_invoked:bash_execute",
    ]
    assert data["elapsed_s"] >= 1.0


async def test_progress_terminal_returns_ok_and_elapsed_frozen(client):
    """Após o término, elapsed_s deve ser o valor gravado (não cresce)."""
    task_id = "done1234"
    worker_server._TASKS[task_id] = {
        "task_id": task_id,
        "ok": True,
        "started_at": "2026-01-01T00:00:00+00:00",
        "brief": "x",
        "phase": "✅ concluído",
        "current_activity": "tool_completed:write_file",
        "progress_lines": ["tool_completed:write_file"],
        "elapsed_s": 42.0,
        "files": ["foo.py", "bar.py"],
    }

    resp = await _get(client, f"/v1/progress/{task_id}")
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["elapsed_s"] == 42.0
    assert data["files"] == ["foo.py", "bar.py"]


async def test_progress_caps_progress_lines_at_30(client):
    """Defensive: o endpoint corta progress_lines em 30 itens (last 30)."""
    task_id = "many"
    worker_server._TASKS[task_id] = {
        "task_id": task_id,
        "ok": None,
        "progress_lines": [f"line {i}" for i in range(100)],
        "_mono_start": time.monotonic(),
    }

    resp = await _get(client, f"/v1/progress/{task_id}")
    data = await resp.json()
    assert len(data["progress_lines"]) == 30
    # Garantia que veio o "final" da lista (line 99 é o último).
    assert data["progress_lines"][-1] == "line 99"


async def test_result_strips_internal_keys(client):
    """``GET /v1/result/{id}`` não deve vazar ``_mono_start`` (chave interna)."""
    task_id = "result-strip"
    worker_server._TASKS[task_id] = {
        "task_id": task_id,
        "ok": True,
        "_mono_start": 12345.0,
        "elapsed_s": 1.0,
        "brief": "x",
    }

    resp = await _get(client, f"/v1/result/{task_id}")
    assert resp.status == 200
    data = await resp.json()
    assert "_mono_start" not in data
    assert data["task_id"] == task_id
