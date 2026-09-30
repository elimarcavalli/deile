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
    # task_id válido (12 chars hex), mas não registrado → 404 NOT_FOUND.
    resp = await _get(client, "/v1/progress/abcdef012345")
    assert resp.status == 404
    data = await resp.json()
    assert data["error"]["code"] == "NOT_FOUND"


async def test_progress_400_when_task_id_invalid_format(client):
    """M10 (PR #295): task_id fora do formato hex de 12 chars retorna 400 BAD_REQUEST.

    Defende contra path traversal e caracteres inesperados antes de tocar
    o filesystem ou usar como dict key.
    """
    # ``..`` é interceptado pelo router (vira ``/v1/`` → 404) — defesa
    # em camadas. Os demais alcançam o handler e devem ser rejeitados como
    # BAD_REQUEST antes de qualquer toque em filesystem.
    for bad in ("does-not-exist", "abcdef01234", "ABCDEF012345", "abcdef0123456"):
        resp = await _get(client, f"/v1/progress/{bad}")
        assert resp.status == 400, f"esperado 400 para task_id={bad!r}"
        data = await resp.json()
        assert data["error"]["code"] == "BAD_REQUEST"


async def test_progress_401_without_bearer(client):
    resp = await client.get("/v1/progress/anything")
    assert resp.status == 401


async def test_progress_401_with_bad_bearer(client):
    resp = await _get(client, "/v1/progress/anything", token="wrong-token-xxxxxxxxx")
    assert resp.status == 401


async def test_progress_midflight_returns_phase_and_progress_lines(client):
    """Simula uma task em execução escrevendo direto no ``_TASKS`` dict."""
    task_id = "abcdef123456"
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
    task_id = "fedcba987654"
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
    task_id = "0123456789ab"
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


async def test_evict_old_tasks_preserves_in_flight():
    """Fix G4: ``_evict_old_tasks_if_needed`` descarta entradas terminais
    quando ``_TASKS`` excede o cap, preservando entradas com ok=None
    (em execução)."""
    worker_server._TASKS.clear()
    original_max = worker_server._TASKS_MAX
    worker_server._TASKS_MAX = 3
    try:
        # 3 terminais + 1 em execução = 4 (> max=3)
        worker_server._TASKS["t1"] = {"ok": True, "finished_at": "2026-01-01T00:00:00"}
        worker_server._TASKS["t2"] = {"ok": True, "finished_at": "2026-01-02T00:00:00"}
        worker_server._TASKS["t3"] = {"ok": False, "finished_at": "2026-01-03T00:00:00"}
        worker_server._TASKS["running"] = {"ok": None}

        worker_server._evict_old_tasks_if_needed()

        # Em execução SEMPRE preservada
        assert "running" in worker_server._TASKS
        # Total <= max (3) — pelo menos a mais antiga foi removida
        assert len(worker_server._TASKS) <= 3
        # A mais antiga (t1) foi a primeira a sair
        assert "t1" not in worker_server._TASKS
    finally:
        worker_server._TASKS.clear()
        worker_server._TASKS_MAX = original_max


async def test_evict_noop_when_only_in_flight_tasks():
    """Se todas as entradas estão em execução, eviction não faz nada (não
    descartamos trabalho ativo)."""
    worker_server._TASKS.clear()
    original_max = worker_server._TASKS_MAX
    worker_server._TASKS_MAX = 1
    try:
        worker_server._TASKS["a"] = {"ok": None}
        worker_server._TASKS["b"] = {"ok": None}
        worker_server._TASKS["c"] = {"ok": None}
        worker_server._evict_old_tasks_if_needed()
        assert len(worker_server._TASKS) == 3
    finally:
        worker_server._TASKS.clear()
        worker_server._TASKS_MAX = original_max


async def test_result_400_when_task_id_invalid_format(client):
    """M10 (PR #295): mesmo guard em ``/v1/result/`` — task_id fora do
    formato hex de 12 chars retorna 400 antes de tocar o filesystem."""
    resp = await _get(client, "/v1/result/not-hex")
    assert resp.status == 400
    data = await resp.json()
    assert data["error"]["code"] == "BAD_REQUEST"


# ── Regression tests para PR #295 review ──────────────────────────────────




async def test_event_bus_unsubscribed_at_dispatch_end(monkeypatch, tmp_path, _clean_tasks):
    """B3 (PR #295 review): o handler wildcard registrado por ``_run_task`` no
    EventBus singleton deve ser removido no ``finally`` quando a task termina.

    Prova COMPORTAMENTAL (substitui o antigo grep de source, que não provava
    nem que ``unsubscribe_all`` era chamado no ``finally`` nem que surtia
    efeito): roda ``_run_task`` com o agente e o post HTTP mockados contra o
    EventBus real e verifica que (a) ``subscribe_all``/``unsubscribe_all`` foram
    de fato invocados e (b) a lista de wildcard handlers volta ao estado
    inicial — sem resíduo pinando estado da task morta.
    """
    import types

    from deile.events.event_bus import get_event_bus

    # Agente falso: SEM ``process_input_stream`` (força o caminho
    # ``process_input``), retorna conteúdo trivial e uma sessão inócua.
    class _FakeAgent:
        async def get_or_create_session(self, session_id, persisted=False):
            return types.SimpleNamespace(context_data={})

        async def process_input(self, prompt, **kwargs):
            return types.SimpleNamespace(content="done")

    async def _fake_get_agent():
        return _FakeAgent()

    async def _fake_post_status(channel_id, text):
        return None

    monkeypatch.setattr(worker_server, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(worker_server, "_get_agent", _fake_get_agent)
    monkeypatch.setattr(worker_server, "_post_status_message", _fake_post_status)

    bus = get_event_bus()  # mesmo singleton que ``_run_task`` resolve internamente
    baseline = list(bus._wildcard_handlers)

    # Spy: confirma que o ciclo subscribe/unsubscribe realmente aconteceu,
    # preservando o comportamento real (delega às implementações originais).
    calls = {"sub": 0, "unsub": 0}
    _real_sub = bus.subscribe_all
    _real_unsub = bus.unsubscribe_all

    def _spy_sub(handler):
        calls["sub"] += 1
        return _real_sub(handler)

    def _spy_unsub(handler):
        calls["unsub"] += 1
        return _real_unsub(handler)

    monkeypatch.setattr(bus, "subscribe_all", _spy_sub)
    monkeypatch.setattr(bus, "unsubscribe_all", _spy_unsub)

    result = await worker_server._run_task(
        task_id="abcdef123456",
        brief="test brief",
        channel_id="chan-1",
        user_message_id=None,
        persona=None,
    )

    assert result["ok"] is True
    # (a) o handler foi de fato inscrito e o ``finally`` o desinscreveu.
    assert calls["sub"] == 1, "_run_task deveria inscrever _on_event no EventBus"
    assert calls["unsub"] == 1, (
        "B3 regression: _run_task deve chamar bus.unsubscribe_all(_on_event) "
        "no finally para evitar handler leak no EventBus singleton"
    )
    # (b) sem resíduo: a lista de wildcard handlers voltou ao estado inicial.
    assert list(bus._wildcard_handlers) == baseline


async def test_event_bus_unsubscribe_all_removes_handler():
    """B3 (PR #295 review): EventBus.unsubscribe_all remove o handler do
    wildcard list — sem isto, handlers stale acumulam no singleton."""
    from deile.events.event_bus import EventBus

    bus = EventBus()

    async def _h(_evt):
        return None

    bus.subscribe_all(_h)
    assert _h in bus._wildcard_handlers
    assert bus.unsubscribe_all(_h) is True
    assert _h not in bus._wildcard_handlers
    # Idempotente: segunda remoção retorna False, não levanta.
    assert bus.unsubscribe_all(_h) is False


async def test_result_strips_internal_keys(client):
    """``GET /v1/result/{id}`` não deve vazar ``_mono_start`` (chave interna)."""
    task_id = "aabbccddeeff"
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


def test_task_id_regex_matches_generated_ids():
    """Iter-2 review: ``_TASK_ID_RE`` deve aceitar IDs gerados pelo próprio
    worker (``uuid.uuid4().hex[:_TASK_ID_LEN]``). Drift entre regex e
    geração silenciosamente rejeitaria task_ids legítimos.
    """
    import uuid

    # Sanity: o _TASK_ID_LEN é usado em ambos os lados.
    assert worker_server._TASK_ID_LEN == 12
    # 10 IDs sintéticos — todos devem casar.
    for _ in range(10):
        tid = uuid.uuid4().hex[: worker_server._TASK_ID_LEN]
        assert worker_server._TASK_ID_RE.match(tid) is not None, (
            f"task_id {tid!r} foi gerado por uuid.hex[:{worker_server._TASK_ID_LEN}] "
            f"mas a regex {worker_server._TASK_ID_RE.pattern!r} o rejeita"
        )
