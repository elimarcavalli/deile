"""AC3 (issue #620) — lock de higiene na seção evict+insert de ``_TASKS``.

FIAÇÃO (HTTP real → handler → estado): um burst de 100 dispatches concorrentes
com ``_TASKS_MAX=10`` mantém ``len(_TASKS) <= 10 + N_em_voo`` ao final. O lock
``_TASKS_LOCK`` torna a invariante de tamanho à prova de regressão se um
``await`` for introduzido entre o evict e o insert no futuro.
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
from worker_rate_limit import TokenBucketRateLimiter  # noqa: E402

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
async def client(_clean_state, monkeypatch):
    # Rate limiter generoso para não interferir no teste de lock.
    monkeypatch.setattr(
        worker_server, "_RATE_LIMITER",
        TokenBucketRateLimiter(capacity=10_000, rate=10_000),
    )

    app = worker_server.build_app(_TOKEN)
    async with aiohttp_test_utils.TestClient(
        aiohttp_test_utils.TestServer(app)
    ) as cli:
        yield cli


async def test_burst_keeps_tasks_within_bound(client, monkeypatch):
    """100 dispatches concorrentes (fire-and-forget), todos em voo durante o
    burst: o evict NUNCA descarta trabalho ativo, logo ``len(_TASKS)`` fica
    dentro de ``10 + N_em_voo`` — invariante AC3 que o lock protege."""
    monkeypatch.setattr(worker_server, "_TASKS_MAX", 10)

    release = asyncio.Event()

    async def _blocking(task_id, brief, channel_id, *a, **kw):
        # ``ok`` permanece None enquanto bloqueado → todas as 100 tasks ficam
        # em voo simultaneamente durante a medição.
        worker_server._TASKS[task_id] = {
            "task_id": task_id, "ok": None, "brief": brief,
        }
        await release.wait()
        result = {"schema_version": worker_server.RESULT_SCHEMA_VERSION,
                  "task_id": task_id, "ok": True, "elapsed_s": 0.0,
                  "finished_at": "2026-01-01T00:00:00+00:00",
                  "brief": brief, "summary": "ok", "files": []}
        worker_server._TASKS[task_id] = result
        return result

    monkeypatch.setattr(worker_server, "_run_task", _blocking)

    async def _one(i):
        return await client.post(
            "/v1/dispatch",
            json={"brief": f"task {i}", "channel_id": f"chan-{i}",
                  "wait_for_result": False},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    # Admite as 100 (cada uma 202; a task fica em voo, bloqueada em ``release``).
    resps = await asyncio.gather(*[_one(i) for i in range(100)])
    assert all(r.status == 202 for r in resps)
    # Deixa o background task de cada dispatch chegar ao ponto de bloqueio.
    await asyncio.sleep(0.05)

    in_flight = sum(
        1 for s in worker_server._TASKS.values()
        if isinstance(s, dict) and s.get("ok") is None
    )
    # AC3: o evict preserva todo o trabalho em voo → bound 10 + N_em_voo.
    assert len(worker_server._TASKS) <= 10 + in_flight
    # Sanidade: o estado tem as 100 tasks em voo (eviction não as tocou).
    assert in_flight == 100

    # Libera para todas terminarem limpas (evita warnings de task pendente).
    release.set()
    await asyncio.sleep(0.05)


async def test_evict_runs_under_lock(client, monkeypatch):
    """O evict da seção crítica DEVE rodar com ``_TASKS_LOCK`` adquirido (AC3).

    Comportamental (não texto-fonte): espiona ``_evict_old_tasks_if_needed`` e
    captura ``_TASKS_LOCK.locked()`` no exato instante da chamada dentro do
    ``dispatch_handler`` — deve ser True. Robusto a rename/refactor do handler.
    """
    monkeypatch.setattr(worker_server, "_TASKS_MAX", 10)

    locked_during_evict: list[bool] = []
    _orig_evict = worker_server._evict_old_tasks_if_needed

    def _spy_evict(*a, **kw):
        locked_during_evict.append(worker_server._TASKS_LOCK.locked())
        return _orig_evict(*a, **kw)

    monkeypatch.setattr(worker_server, "_evict_old_tasks_if_needed", _spy_evict)

    async def _noop(task_id, brief, channel_id, *a, **kw):
        return {"schema_version": worker_server.RESULT_SCHEMA_VERSION,
                "task_id": task_id, "ok": True, "elapsed_s": 0.0,
                "finished_at": "2026-01-01T00:00:00+00:00",
                "brief": brief, "summary": "ok", "files": []}

    monkeypatch.setattr(worker_server, "_run_task", _noop)

    resp = await client.post(
        "/v1/dispatch",
        json={"brief": "task", "channel_id": "chan", "wait_for_result": False},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status == 202
    await asyncio.sleep(0.05)

    # O evict foi chamado e, em TODA chamada, o lock estava adquirido.
    assert locked_during_evict, "_evict_old_tasks_if_needed não foi chamado"
    assert all(locked_during_evict), (
        "_evict_old_tasks_if_needed rodou SEM _TASKS_LOCK adquirido"
    )
