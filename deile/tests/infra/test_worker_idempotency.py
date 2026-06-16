"""AC6 (issue #620) — idempotência de dispatch via ``X-Idempotency-Key``.

FIAÇÃO (HTTP real → handler → estado): 2 POSTs com a mesma key dentro do TTL
devem servir o resultado original (200 + ``already_dispatched``); uma key de
task ainda em execução → 409 ``duplicate_in_flight``; uma key expirada (>300s)
→ nova task.
"""

from __future__ import annotations

import asyncio
import sys
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
def _clean_state():
    worker_server._TASKS.clear()
    worker_server._IDEMPOTENCY_KEYS.clear()
    worker_server._IN_FLIGHT = 0
    worker_server._SHUTTING_DOWN = False
    worker_server._METRICS.reset()
    yield
    worker_server._TASKS.clear()
    worker_server._IDEMPOTENCY_KEYS.clear()
    worker_server._IN_FLIGHT = 0
    worker_server._SHUTTING_DOWN = False
    worker_server._METRICS.reset()


@pytest.fixture
async def client(_clean_state):
    app = worker_server.build_app(_TOKEN)
    async with aiohttp_test_utils.TestClient(aiohttp_test_utils.TestServer(app)) as cli:
        yield cli


def _fake_run_task(ok=True):
    async def _fake(task_id, brief, channel_id, *a, **kw):
        result = {
            "schema_version": worker_server.RESULT_SCHEMA_VERSION,
            "task_id": task_id,
            "ok": ok,
            "elapsed_s": 0.01,
            "brief": brief,
            "summary": "done",
            "files": [],
            "channel_id": channel_id,
        }
        worker_server._TASKS[task_id] = result
        return result

    return _fake


async def _post(client, body, *, key=None):
    h = {"Authorization": f"Bearer {_TOKEN}"}
    if key is not None:
        h["X-Idempotency-Key"] = key
    return await client.post("/v1/dispatch", json=body, headers=h)


async def test_same_key_returns_already_dispatched(client, monkeypatch):
    monkeypatch.setattr(worker_server, "_run_task", _fake_run_task(True))
    body = {"brief": "do it", "channel_id": "111"}

    r1 = await _post(client, body, key="abc-key-1")
    assert r1.status == 200
    d1 = await r1.json()
    first_task_id = d1["task_id"]

    r2 = await _post(client, body, key="abc-key-1")
    assert r2.status == 200
    d2 = await r2.json()
    assert d2["status"] == "already_dispatched"
    # Mesma task original — não criou outra.
    assert d2["task_id"] == first_task_id


async def test_in_flight_key_returns_409(client, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_run_task(task_id, brief, channel_id, *a, **kw):
        # ``ok`` permanece None enquanto bloqueado → estado "em execução".
        worker_server._TASKS[task_id] = {
            "task_id": task_id,
            "ok": None,
            "brief": brief,
        }
        started.set()
        await release.wait()
        result = {
            "schema_version": worker_server.RESULT_SCHEMA_VERSION,
            "task_id": task_id,
            "ok": True,
            "elapsed_s": 0.01,
            "brief": brief,
            "summary": "done",
            "files": [],
        }
        worker_server._TASKS[task_id] = result
        return result

    monkeypatch.setattr(worker_server, "_run_task", _blocking_run_task)

    # Fire-and-forget para a 1ª task ficar em execução de verdade.
    r1 = await _post(
        client,
        {"brief": "x", "channel_id": "222", "wait_for_result": False},
        key="same-key",
    )
    assert r1.status == 202
    await asyncio.wait_for(started.wait(), timeout=2.0)

    # 2ª com a mesma key, enquanto a 1ª está em voo → 409.
    r2 = await _post(
        client,
        {"brief": "x", "channel_id": "222", "wait_for_result": False},
        key="same-key",
    )
    assert r2.status == 409
    d2 = await r2.json()
    assert d2["error"]["code"] == "duplicate_in_flight"

    # Libera a 1ª para terminar limpo.
    release.set()
    await asyncio.sleep(0.05)


async def test_expired_key_creates_new_task(client, monkeypatch):
    monkeypatch.setattr(worker_server, "_run_task", _fake_run_task(True))
    # TTL curtíssimo para forçar expiração determinística.
    monkeypatch.setattr(worker_server, "_IDEMPOTENCY_TTL_S", 0.0)
    body = {"brief": "do it", "channel_id": "333"}

    r1 = await _post(client, body, key="ttl-key")
    first_id = (await r1.json())["task_id"]

    # Com TTL=0, a entrada já está expirada no próximo lookup → nova task.
    r2 = await _post(client, body, key="ttl-key")
    assert r2.status == 200
    d2 = await r2.json()
    assert d2.get("status") != "already_dispatched"
    assert d2["task_id"] != first_id


async def test_no_key_always_new_task(client, monkeypatch):
    monkeypatch.setattr(worker_server, "_run_task", _fake_run_task(True))
    body = {"brief": "do it", "channel_id": "444"}
    r1 = await _post(client, body)
    r2 = await _post(client, body)
    id1 = (await r1.json())["task_id"]
    id2 = (await r2.json())["task_id"]
    assert id1 != id2  # sem key, cada POST é uma task nova (compat legado)


async def test_malformed_key_ignored_as_absent(client, monkeypatch):
    monkeypatch.setattr(worker_server, "_run_task", _fake_run_task(True))
    body = {"brief": "do it", "channel_id": "555"}
    # Key com caractere ilegal (espaço) → tratada como ausente → task nova.
    r1 = await _post(client, body, key="bad key!")
    r2 = await _post(client, body, key="bad key!")
    id1 = (await r1.json())["task_id"]
    id2 = (await r2.json())["task_id"]
    assert id1 != id2
