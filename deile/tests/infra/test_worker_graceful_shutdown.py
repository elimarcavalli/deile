"""AC1 (issue #620) — graceful shutdown do deile-worker no SIGTERM.

FIAÇÃO: ``_graceful_shutdown`` marca ``_SHUTTING_DOWN``, drena
``_BG_DISPATCH_TASKS`` com timeout de 30s e escreve estado terminal nas tasks
não concluídas; um dispatch HTTP chegando durante o shutdown é rejeitado com
503. O watchdog de hard-deadline chama ``os._exit(0)`` (mockado) se o drain
estourar 35s.
"""

from __future__ import annotations

import asyncio
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
def _clean_state():
    worker_server._TASKS.clear()
    worker_server._BG_DISPATCH_TASKS.clear()
    worker_server._SHUTTING_DOWN = False
    yield
    worker_server._TASKS.clear()
    worker_server._BG_DISPATCH_TASKS.clear()
    worker_server._SHUTTING_DOWN = False


# ----- drain de tasks em voo + estado terminal -----------------------------


async def test_graceful_shutdown_drains_and_marks_terminal(_clean_state):
    done = asyncio.Event()

    async def _bg():
        worker_server._TASKS["aaaaaaaaaaaa"] = {
            "task_id": "aaaaaaaaaaaa",
            "ok": None,
        }
        await asyncio.sleep(0.02)
        worker_server._TASKS["aaaaaaaaaaaa"] = {
            "task_id": "aaaaaaaaaaaa",
            "ok": True,
        }
        done.set()

    t = asyncio.create_task(_bg())
    worker_server._BG_DISPATCH_TASKS.add(t)
    await asyncio.sleep(0)  # deixa a task começar e registrar ok=None

    start = time.monotonic()
    await worker_server._graceful_shutdown(app=None)
    elapsed = time.monotonic() - start

    # Drena dentro do orçamento e MUITO antes do teto de 35s.
    assert elapsed < 35.0
    assert worker_server._SHUTTING_DOWN is True
    # A task drenou naturalmente (terminou antes do timeout).
    assert done.is_set()
    assert worker_server._TASKS["aaaaaaaaaaaa"]["ok"] is True


async def test_drain_timeout_marks_remaining_terminal(_clean_state, monkeypatch):
    # Drain timeout curtíssimo para forçar o caminho de "marcar terminal".
    monkeypatch.setattr(worker_server, "_SHUTDOWN_DRAIN_TIMEOUT_S", 0.05)

    release = asyncio.Event()

    async def _bg():
        worker_server._TASKS["bbbbbbbbbbbb"] = {
            "task_id": "bbbbbbbbbbbb",
            "ok": None,
        }
        await release.wait()  # nunca liberado dentro do drain

    t = asyncio.create_task(_bg())
    worker_server._BG_DISPATCH_TASKS.add(t)
    await asyncio.sleep(0)

    await worker_server._graceful_shutdown(app=None)

    # A task não terminou; o shutdown a marcou terminal explicitamente (AC1).
    state = worker_server._TASKS["bbbbbbbbbbbb"]
    assert state["ok"] is False
    assert state["error"] == "worker shutting down"

    # Limpa a task pendente.
    release.set()
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass


def test_mark_inflight_tasks_terminal(_clean_state):
    worker_server._TASKS["t1"] = {"task_id": "t1", "ok": None}
    worker_server._TASKS["t2"] = {"task_id": "t2", "ok": True}  # já terminal
    worker_server._TASKS["t3"] = {"task_id": "t3", "ok": None}

    marked = worker_server._mark_inflight_tasks_terminal()
    assert marked == 2
    assert worker_server._TASKS["t1"]["ok"] is False
    assert worker_server._TASKS["t1"]["error"] == "worker shutting down"
    assert worker_server._TASKS["t2"]["ok"] is True  # intacto


# ----- 503 para novos dispatches durante o shutdown ------------------------


@pytest.fixture
async def client(_clean_state):
    app = worker_server.build_app(_TOKEN)
    async with aiohttp_test_utils.TestClient(aiohttp_test_utils.TestServer(app)) as cli:
        yield cli


async def test_dispatch_rejected_503_while_shutting_down(client, monkeypatch):
    monkeypatch.setattr(worker_server, "_SHUTTING_DOWN", True)
    resp = await client.post(
        "/v1/dispatch",
        json={"brief": "late", "channel_id": "111"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status == 503
    data = await resp.json()
    assert data["error"]["code"] == "SHUTTING_DOWN"
    assert resp.headers.get("Retry-After")


# ----- watchdog hard-deadline (os._exit mockado) ---------------------------


def test_watchdog_calls_os_exit_after_deadline(monkeypatch):
    """Ao receber SIGTERM, o watchdog arma um timer que chama os._exit(0)
    após o hard-deadline. Mockamos ``os._exit`` para não matar o runner e
    encurtamos o deadline para validar o disparo."""
    import signal

    exits: list = []
    monkeypatch.setattr(worker_server.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(worker_server, "_SHUTDOWN_HARD_DEADLINE_S", 0.05)
    worker_server._SHUTTING_DOWN = False

    captured = {}

    def _fake_signal(signum, handler):
        captured["handler"] = handler

    monkeypatch.setattr(signal, "signal", _fake_signal)
    worker_server._install_shutdown_watchdog()
    assert "handler" in captured

    # Dispara o handler como se o SIGTERM tivesse chegado.
    captured["handler"](signal.SIGTERM, None)
    assert worker_server._SHUTTING_DOWN is True

    # O timer dispara os._exit(0) após ~50ms.
    time.sleep(0.2)
    assert exits == [0]
    worker_server._SHUTTING_DOWN = False
