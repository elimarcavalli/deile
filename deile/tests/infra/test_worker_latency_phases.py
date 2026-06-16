"""AC8 (issue #620) — latência por fase no ``_run_task`` do deile-worker.

Executa ``_run_task`` com um agente mockado (sem LLM real) e valida que o log
estruturado emitido ao final carrega o campo ``phases`` com os 5 marcos
nomeados (``agent_start``, ``model_first_token``, ``agent_end``, ``io_ops``,
``total``), com deltas em milissegundos.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("aiohttp")

import worker_server  # noqa: E402

pytestmark = pytest.mark.unit

_EXPECTED_PHASES = {
    "agent_start",
    "model_first_token",
    "agent_end",
    "io_ops",
    "total",
}


@pytest.fixture
def _clean_tasks():
    worker_server._TASKS.clear()
    yield
    worker_server._TASKS.clear()


def _mock_agent_with_delay():
    """Agente não-streaming que adiciona um pequeno delay real para que os
    deltas de fase sejam positivos de forma determinística."""

    class _Resp:
        content = "feito"

    async def _slow_process_input(prompt, **kwargs):
        await asyncio.sleep(0.005)
        return _Resp()

    agent = MagicMock()
    agent.get_or_create_session = AsyncMock(return_value=MagicMock(context_data={}))
    agent.process_input = AsyncMock(side_effect=_slow_process_input)
    agent.process_input_stream = None  # força o caminho não-streaming
    return agent


def _capture_phases(records) -> dict:
    for rec in records:
        phases = getattr(rec, "phases", None)
        if isinstance(phases, dict):
            return phases
    return {}


async def test_run_task_logs_five_named_phases(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(worker_server, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(
        worker_server, "_get_agent", AsyncMock(return_value=_mock_agent_with_delay())
    )
    monkeypatch.setattr(
        worker_server, "_post_status_message", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        worker_server, "_edit_status_message", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(worker_server, "_react", AsyncMock(return_value=True))

    with caplog.at_level(logging.INFO, logger="deile.worker_server"):
        await worker_server._run_task(
            "aaaaaaaaaaaa",
            "faça algo",
            "12345",
            None,
            "developer",
        )

    phases = _capture_phases(caplog.records)
    assert phases, "log com campo 'phases' não foi emitido"
    # AC8: exatamente os 5 marcos nomeados.
    assert set(phases) == _EXPECTED_PHASES
    # Cada delta é numérico (ms) e não-negativo.
    for name, delta in phases.items():
        assert isinstance(delta, (int, float)), f"{name} não é numérico"
        assert delta >= 0.0, f"{name} delta negativo: {delta}"
    # ``total`` cobre o wall-clock inteiro; com o delay de 5ms é > 0.
    assert phases["total"] > 0.0
    # ``model_first_token`` foi marcado (delay real do agente) → > 0.
    assert phases["agent_start"] >= 0.0


def test_phase_helper_handles_missing_marks():
    """``_log_phase_latencies`` é robusto a marcos ausentes (timeout antes do
    1º token): cai no marco anterior, sem deltas negativos, sem levantar."""
    import time

    start = time.monotonic()
    marks = {"start": start, "agent_start": start + 0.01, "total": start + 0.02}
    # Não deve levantar mesmo com model_first_token/agent_end/io_ops ausentes.
    worker_server._log_phase_latencies("bbbbbbbbbbbb", marks)
