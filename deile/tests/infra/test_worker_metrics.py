"""AC2 (issue #620) — endpoint ``GET /v1/metrics`` do deile-worker.

FIAÇÃO: POST /v1/dispatch (com ``_run_task`` mockado para não chamar LLM) →
GET /v1/metrics → parse do Prometheus text format → valida as métricas
nomeadas + o histograma skeleton (``_count=0``).
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


def _fake_run_task_factory(ok: bool = True):
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


def _parse_prom(text: str) -> dict:
    """Parse mínimo do text format: ``metric{labels} value`` → {name: value}.

    Linhas ``# HELP``/``# TYPE`` são ignoradas. Para métricas com labels o
    nome é a parte antes de ``{``.
    """
    out: dict = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name_part, _, value = line.rpartition(" ")
        name = name_part.split("{", 1)[0]
        out.setdefault(name, []).append((name_part, value))
    return out


async def _post(client, body, headers=None):
    h = {"Authorization": f"Bearer {_TOKEN}"}
    if headers:
        h.update(headers)
    return await client.post("/v1/dispatch", json=body, headers=h)


async def _get_metrics(client):
    resp = await client.get(
        "/v1/metrics", headers={"Authorization": f"Bearer {_TOKEN}"}
    )
    return resp


async def test_metrics_requires_auth(client):
    resp = await client.get("/v1/metrics")
    assert resp.status == 401


async def test_metrics_content_type_and_named_metrics(client, monkeypatch):
    monkeypatch.setattr(worker_server, "_run_task", _fake_run_task_factory(True))
    r = await _post(
        client,
        {
            "brief": "do it",
            "channel_id": "111",
            "stage": "implement",
            "persona": "developer",
        },
    )
    assert r.status == 200

    resp = await _get_metrics(client)
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"

    text = await resp.text()
    # AC2: pelo menos as 5 métricas nomeadas presentes.
    for name in (
        "deile_worker_dispatches_total",
        "deile_worker_errors_total",
        "deile_worker_in_flight",
        "deile_worker_tasks_memory",
        "deile_worker_dispatch_duration_seconds",
    ):
        assert name in text, f"métrica {name} ausente no /v1/metrics"


async def test_dispatch_counter_increments_with_labels(client, monkeypatch):
    monkeypatch.setattr(worker_server, "_run_task", _fake_run_task_factory(True))
    await _post(
        client,
        {
            "brief": "a",
            "channel_id": "222",
            "stage": "implement",
            "persona": "developer",
        },
    )

    text = await (await _get_metrics(client)).text()
    parsed = _parse_prom(text)
    series = parsed["deile_worker_dispatches_total"]
    # Há uma série com labels stage/persona/ok="true".
    matched = [s for s in series if 'stage="implement"' in s[0] and 'ok="true"' in s[0]]
    assert matched, f"série com labels esperados ausente: {series}"
    assert matched[0][1] == "1"


async def test_histogram_skeleton_count_zero(client):
    """Histograma skeleton: presente, com buckets e ``_count=0`` / ``_sum=0``."""
    text = await (await _get_metrics(client)).text()
    assert "deile_worker_dispatch_duration_seconds_count 0" in text
    assert "deile_worker_dispatch_duration_seconds_sum 0" in text
    # Buckets definidos (le="1" ... le="+Inf").
    assert 'deile_worker_dispatch_duration_seconds_bucket{le="1"} 0' in text
    assert 'deile_worker_dispatch_duration_seconds_bucket{le="+Inf"} 0' in text


async def test_in_flight_gauge_zero_when_idle(client):
    text = await (await _get_metrics(client)).text()
    parsed = _parse_prom(text)
    assert parsed["deile_worker_in_flight"][0][1] == "0"


# ----- packaging guard (issue #620) ---------------------------------------
#
# worker_server.py importa ``worker_metrics`` e ``worker_rate_limit`` por nome
# nu — ambos PRECISAM ser baked em /app (COPY no Dockerfile + exceção no
# .dockerignore), senão o deile-worker pod crasha na importação no startup.
# Mesma disciplina de test_monitor_packaging.py (incidente do cost-ledger).


@pytest.mark.parametrize("mod", ("worker_metrics.py", "worker_rate_limit.py"))
def test_dockerfile_copies_worker_hardening_module(mod):
    dockerfile = (_REPO / "Dockerfile").read_text(encoding="utf-8")
    assert (
        f"COPY --chown=deile:deile infra/k8s/{mod} /app/{mod}" in dockerfile
    ), f"{mod} deve ser COPY'd para /app no Dockerfile ou o pod crasha no import"


@pytest.mark.parametrize("mod", ("worker_metrics.py", "worker_rate_limit.py"))
def test_dockerignore_excepts_worker_hardening_module(mod):
    dockerignore = (_REPO / ".dockerignore").read_text(encoding="utf-8")
    assert (
        f"!infra/k8s/{mod}" in dockerignore
    ), f"{mod} precisa de exceção `!infra/k8s/{mod}` no .dockerignore ou o COPY falha"
