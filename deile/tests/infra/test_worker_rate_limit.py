"""AC7 (issue #620) — rate limit por canal (token bucket capacity=10, rate=1/s).

FIAÇÃO (HTTP real → handler → 429): um burst de 15 dispatches do MESMO canal
em <1s produz >=5 rejeições 429 com header ``Retry-After``; um canal diferente
não é afetado (0 rejeições). Cobre também a unidade do
:class:`TokenBucketRateLimiter` (refil, isolamento, reset de balde ocioso).
"""
from __future__ import annotations

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


# ----- unidade do token bucket --------------------------------------------


class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def test_burst_consumes_capacity_then_rejects():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=10, rate=1, time_source=clock)
    allowed = sum(1 for _ in range(10) if rl.acquire("c1")[0])
    assert allowed == 10  # capacidade cheia consumida
    ok, retry_after = rl.acquire("c1")
    assert ok is False
    assert retry_after >= 1  # Retry-After em segundos


def test_refill_over_time():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=10, rate=1, time_source=clock)
    for _ in range(10):
        rl.acquire("c1")
    assert rl.acquire("c1")[0] is False
    clock.advance(2.0)  # 2 tokens recarregados
    assert rl.acquire("c1")[0] is True
    assert rl.acquire("c1")[0] is True
    assert rl.acquire("c1")[0] is False


def test_channels_are_isolated():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(capacity=10, rate=1, time_source=clock)
    for _ in range(10):
        rl.acquire("c1")
    assert rl.acquire("c1")[0] is False  # c1 esgotado
    assert rl.acquire("c2")[0] is True   # c2 intacto


def test_idle_bucket_resets_to_full():
    clock = _FakeClock()
    rl = TokenBucketRateLimiter(
        capacity=10, rate=1, idle_reset_s=300.0, time_source=clock,
    )
    for _ in range(10):
        rl.acquire("c1")
    assert rl.acquire("c1")[0] is False
    clock.advance(301.0)  # ocioso > 300s → reset para cheio
    # Próximo acesso reseta para capacity; 10 dispatches passam de novo.
    allowed = sum(1 for _ in range(10) if rl.acquire("c1")[0])
    assert allowed == 10


def test_invalid_params_rejected():
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=0)
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(rate=0)


# ----- FIAÇÃO HTTP ---------------------------------------------------------


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
    # Rate limiter fresco por teste (o módulo usa um singleton).
    fresh = TokenBucketRateLimiter(capacity=10, rate=1, idle_reset_s=300.0)
    monkeypatch.setattr(worker_server, "_RATE_LIMITER", fresh)

    async def _fake(task_id, brief, channel_id, *a, **kw):
        result = {"schema_version": worker_server.RESULT_SCHEMA_VERSION,
                  "task_id": task_id, "ok": True, "elapsed_s": 0.01,
                  "brief": brief, "summary": "ok", "files": []}
        worker_server._TASKS[task_id] = result
        return result

    monkeypatch.setattr(worker_server, "_run_task", _fake)

    app = worker_server.build_app(_TOKEN)
    async with aiohttp_test_utils.TestClient(
        aiohttp_test_utils.TestServer(app)
    ) as cli:
        yield cli


async def _post(client, channel_id):
    return await client.post(
        "/v1/dispatch",
        json={"brief": "do it", "channel_id": channel_id,
              "wait_for_result": False},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )


async def test_burst_same_channel_yields_429s_with_retry_after(client):
    statuses = []
    retry_afters = []
    for _ in range(15):
        r = await _post(client, "chan-A")
        statuses.append(r.status)
        if r.status == 429:
            retry_afters.append(r.headers.get("Retry-After"))

    rejected = sum(1 for s in statuses if s == 429)
    accepted = sum(1 for s in statuses if s == 202)
    assert accepted == 10  # capacity
    assert rejected >= 5    # AC7: >= 5 rejeições
    # Todas as 429 trazem Retry-After.
    assert all(ra is not None for ra in retry_afters)


async def test_other_channel_not_affected(client):
    # Esgota o canal A.
    for _ in range(15):
        await _post(client, "chan-A")
    # Canal B não sofre nenhuma rejeição no seu próprio burst de 10.
    b_rejected = 0
    for _ in range(10):
        r = await _post(client, "chan-B")
        if r.status == 429:
            b_rejected += 1
    assert b_rejected == 0
