"""Decisão #46 — backoff exponencial para ``WORKER_AUTH_EXPIRED``.

Antes do fix, qualquer falha de OAuth bloqueava a issue/PR no primeiro
hit — surtos curtos de auth expirado (refresh in-pod) queimavam trabalho
que poderia ter continuado segundos depois. O fix adiciona um backoff
exponencial por-target: até N-1 falhas são apenas registradas; a partir
da N-ésima, o target entra em pausa com janela ``min(2^count * 60, 1800)``s.
O primeiro sucesso reseta tudo.
"""

from __future__ import annotations

import time

from deile.orchestration.pipeline.stages import (
    _AUTH_BACKOFF_THRESHOLD,
    is_target_auth_paused,
    record_auth_failure_and_maybe_pause,
    reset_auth_failures,
)


class _FakeMonitor:
    """Mock minimal: só precisa dos dois dicts que o backoff toca."""

    def __init__(self) -> None:
        self._auth_failures_by_target: dict[str, int] = {}
        self._paused_until_ts: dict[str, float] = {}


class TestBackoffBelowThresholdDoesNotPause:
    """As primeiras (threshold - 1) falhas não geram pausa.

    Surtos curtos de auth — típicos durante refresh in-pod do OAuth
    do claude — não devem bloquear o target.
    """

    def test_single_failure_does_not_pause(self):
        m = _FakeMonitor()
        count, pause_s = record_auth_failure_and_maybe_pause(m, "issue", 7)
        assert count == 1
        assert pause_s == 0.0
        assert not is_target_auth_paused(m, "issue", 7)

    def test_failures_below_threshold_do_not_pause(self):
        m = _FakeMonitor()
        for i in range(1, _AUTH_BACKOFF_THRESHOLD):
            count, pause_s = record_auth_failure_and_maybe_pause(m, "issue", 7)
            assert count == i
            assert pause_s == 0.0
            assert not is_target_auth_paused(m, "issue", 7)


class TestBackoffAtAndAboveThresholdPauses:
    """A partir da threshold-ésima falha, o target entra em pausa.

    A duração cresce exponencialmente (``2^count * 60``s) com cap em 30min.
    """

    def test_threshold_failure_pauses_target(self):
        m = _FakeMonitor()
        for _ in range(_AUTH_BACKOFF_THRESHOLD):
            count, pause_s = record_auth_failure_and_maybe_pause(m, "pr", 100)
        # Na N-ésima chamada count >= threshold → pause > 0.
        assert count == _AUTH_BACKOFF_THRESHOLD
        assert pause_s > 0
        assert is_target_auth_paused(m, "pr", 100)

    def test_pause_duration_grows_exponentially(self):
        m = _FakeMonitor()
        for _ in range(_AUTH_BACKOFF_THRESHOLD):
            record_auth_failure_and_maybe_pause(m, "pr", 200)
        # 4ª falha: pausa ainda maior.
        m2 = _FakeMonitor()
        for _ in range(_AUTH_BACKOFF_THRESHOLD + 1):
            _, last_pause = record_auth_failure_and_maybe_pause(m2, "pr", 200)
        # A pausa da 4ª chamada deve ser >= dobro da 3ª (2^4 * 60 vs 2^3 * 60).
        assert last_pause >= 60 * (2 ** _AUTH_BACKOFF_THRESHOLD)

    def test_pause_duration_caps_at_max(self):
        from deile.orchestration.pipeline.stages import _AUTH_BACKOFF_MAX_S
        m = _FakeMonitor()
        # Gera muitas falhas para garantir que ``2^count * 60 > MAX``.
        for _ in range(20):
            _, pause_s = record_auth_failure_and_maybe_pause(m, "pr", 999)
        # Última pausa precisa estar capada.
        assert pause_s == _AUTH_BACKOFF_MAX_S


class TestIsTargetAuthPausedExpiresWindow:
    """Quando a janela de pausa expira (``now >= paused_until_ts``), o
    target é considerado liberado — o contador é preservado para escalação
    posterior se a falha persistir.
    """

    def test_expired_window_releases_target(self):
        m = _FakeMonitor()
        # Põe um paused_until_ts no passado.
        m._paused_until_ts["issue:42"] = time.monotonic() - 10
        m._auth_failures_by_target["issue:42"] = _AUTH_BACKOFF_THRESHOLD
        assert not is_target_auth_paused(m, "issue", 42)
        # Contador NÃO foi resetado (próxima falha continua escalando).
        assert m._auth_failures_by_target.get("issue:42") == _AUTH_BACKOFF_THRESHOLD

    def test_active_window_keeps_target_paused(self):
        m = _FakeMonitor()
        m._paused_until_ts["issue:42"] = time.monotonic() + 60
        assert is_target_auth_paused(m, "issue", 42)


class TestResetAuthFailuresClearsState:
    """``reset_auth_failures`` é chamado pelos stage handlers quando um
    dispatch retorna ok=True (sucesso real). Limpa contador E pausa.
    """

    def test_reset_clears_counter_and_pause(self):
        m = _FakeMonitor()
        for _ in range(_AUTH_BACKOFF_THRESHOLD):
            record_auth_failure_and_maybe_pause(m, "issue", 1)
        assert "issue:1" in m._auth_failures_by_target
        assert "issue:1" in m._paused_until_ts
        reset_auth_failures(m, "issue", 1)
        assert "issue:1" not in m._auth_failures_by_target
        assert "issue:1" not in m._paused_until_ts

    def test_reset_is_idempotent(self):
        """Chamar reset sem estado prévio não deve levantar."""
        m = _FakeMonitor()
        reset_auth_failures(m, "issue", 1)  # no-op


class TestBackoffIsPerTarget:
    """Cada target (issue:N, pr:N) tem seu próprio contador — um surto
    em #1 não afeta #2.
    """

    def test_failures_in_one_target_do_not_pause_another(self):
        m = _FakeMonitor()
        for _ in range(_AUTH_BACKOFF_THRESHOLD):
            record_auth_failure_and_maybe_pause(m, "issue", 1)
        assert is_target_auth_paused(m, "issue", 1)
        # Outro target permanece liberado.
        assert not is_target_auth_paused(m, "issue", 2)
        assert not is_target_auth_paused(m, "pr", 1)

    def test_pr_and_issue_with_same_number_are_distinct(self):
        m = _FakeMonitor()
        for _ in range(_AUTH_BACKOFF_THRESHOLD):
            record_auth_failure_and_maybe_pause(m, "pr", 50)
        assert is_target_auth_paused(m, "pr", 50)
        assert not is_target_auth_paused(m, "issue", 50)
