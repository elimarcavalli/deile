"""AC5 (issue #620) — circuit breaker 3-state do cliente do deile-worker.

Exercita :class:`deile.infrastructure.circuit_breaker.CircuitBreaker` com uma
fonte de tempo injetada (fake clock) para validar as transições sem ``sleep``
real:

    closed --(5 falhas)--> open
    open --(< reset)--> rejeita (allow=False)
    open --(>= reset)--> half-open (allow libera 1 probe)
    half-open --(probe OK)--> closed
    half-open --(probe falha)--> open

Inclui um teste de FIAÇÃO no cliente (``DeileWorkerClient.dispatch``): após
abrir o breaker, o próximo dispatch falha com ``CIRCUIT_OPEN`` SEM tocar a
rede (o MockTransport não é chamado).
"""

from __future__ import annotations

import httpx
import pytest

from deile.infrastructure import deile_worker_client as wc
from deile.infrastructure.circuit_breaker import CircuitBreaker, CircuitState
from deile.infrastructure.deile_worker_client import (
    DeileWorkerClient,
    WorkerDispatchError,
    reset_circuit_breaker,
)

pytestmark = pytest.mark.unit


class _FakeClock:
    """Relógio monotônico controlável para a janela de reset."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _breaker(clock: _FakeClock) -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=5,
        reset_timeout_s=30.0,
        time_source=clock,
    )


async def test_starts_closed_and_allows():
    cb = _breaker(_FakeClock())
    assert cb.state is CircuitState.CLOSED
    assert await cb.allow() is True


async def test_five_consecutive_failures_open_the_circuit():
    cb = _breaker(_FakeClock())
    for _ in range(4):
        await cb.record_failure()
        assert cb.state is CircuitState.CLOSED  # ainda fechado em 4 falhas
    await cb.record_failure()  # 5ª falha
    assert cb.state is CircuitState.OPEN
    # Em OPEN dentro da janela, allow rejeita (caller falha sem I/O).
    assert await cb.allow() is False


async def test_success_resets_failure_count():
    cb = _breaker(_FakeClock())
    for _ in range(4):
        await cb.record_failure()
    await cb.record_success()  # zera o contador
    assert cb.state is CircuitState.CLOSED
    for _ in range(4):
        await cb.record_failure()
    # Só 4 falhas após o reset → ainda fechado.
    assert cb.state is CircuitState.CLOSED


async def test_half_open_after_reset_timeout():
    clock = _FakeClock()
    cb = _breaker(clock)
    for _ in range(5):
        await cb.record_failure()
    assert cb.state is CircuitState.OPEN
    # Antes da janela: continua rejeitando.
    clock.advance(29.0)
    assert await cb.allow() is False
    assert cb.state is CircuitState.OPEN
    # Passados 30s: o próximo allow libera o probe e move para half-open.
    clock.advance(1.0)
    assert await cb.allow() is True
    assert cb.state is CircuitState.HALF_OPEN


async def test_half_open_probe_success_closes():
    clock = _FakeClock()
    cb = _breaker(clock)
    for _ in range(5):
        await cb.record_failure()
    clock.advance(30.0)
    assert await cb.allow() is True  # → half-open
    await cb.record_success()  # probe OK
    assert cb.state is CircuitState.CLOSED
    assert await cb.allow() is True


async def test_half_open_probe_failure_reopens():
    clock = _FakeClock()
    cb = _breaker(clock)
    for _ in range(5):
        await cb.record_failure()
    clock.advance(30.0)
    assert await cb.allow() is True  # → half-open
    await cb.record_failure()  # probe falha
    assert cb.state is CircuitState.OPEN
    # E reabre a janela: imediatamente após, allow rejeita de novo.
    assert await cb.allow() is False
    # Após nova janela de 30s, libera novo probe.
    clock.advance(30.0)
    assert await cb.allow() is True
    assert cb.state is CircuitState.HALF_OPEN


def test_state_int_values_match_gauge():
    """Os valores inteiros são o contrato da métrica gauge (0/1/2)."""
    assert int(CircuitState.CLOSED) == 0
    assert int(CircuitState.OPEN) == 1
    assert int(CircuitState.HALF_OPEN) == 2


def test_threshold_must_be_positive():
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)


# ----- FIAÇÃO no cliente: breaker aberto rejeita sem I/O -------------------


def _install_mock_transport(monkeypatch, handler) -> DeileWorkerClient:
    monkeypatch.setattr(wc, "_read_token", lambda: "a-valid-token-123")
    monkeypatch.setattr(wc, "_resolve_endpoint", lambda: "http://mock.invalid")
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return DeileWorkerClient()


async def test_dispatch_rejects_with_circuit_open_without_io(monkeypatch):
    """Após 5 falhas, o dispatch seguinte falha com ``CIRCUIT_OPEN`` e o
    transporte NÃO é chamado (prova de que não há I/O em OPEN)."""
    reset_circuit_breaker()

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(wc.asyncio, "sleep", _no_sleep)

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    cli = _install_mock_transport(monkeypatch, handler)
    payload = {"brief": "hi", "channel_id": "c"}

    # 5 dispatches que falham: cada um esgota o retry (3 tentativas) e conta
    # uma falha consecutiva no breaker. 1 dispatch falho → 1 record_failure.
    # Precisamos de 5 record_failure para abrir → 5 dispatches.
    for _ in range(5):
        with pytest.raises(WorkerDispatchError):
            await cli.dispatch(payload, wait=False)

    calls_after_open = calls["n"]
    # Próximo dispatch: breaker aberto → CIRCUIT_OPEN sem novo hit no handler.
    with pytest.raises(WorkerDispatchError) as ei:
        await cli.dispatch(payload, wait=False)
    assert ei.value.error_code == "CIRCUIT_OPEN"
    assert calls["n"] == calls_after_open  # nenhuma chamada de rede nova

    reset_circuit_breaker()
