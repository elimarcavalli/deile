"""Scale-to-zero on-demand dos CLI workers (plano B5 / finding 3).

Cobre :mod:`deile.orchestration.pipeline.cli_worker_scaler`:
  - dispatcher núcleo (deile/claude) → NOT_APPLICABLE (nunca escala);
  - 0 réplicas → ``kubectl scale --replicas=1`` (SCALED);
  - ≥1 réplica → READY (nada a fazer);
  - scale falha (RBAC/erro) → SCALE_FAILED + ``ok_to_dispatch=False``;
  - kubectl ausente → NO_KUBECTL + erro instrutivo;
  - cooldown anti-flapping entre ticks.

E o wiring no :class:`WorkerImplementer._dispatch`: um dispatch para CLI worker
com 0 réplicas dispara o ensure-replica ANTES do POST; falha de scale devolve
``WorkOutcome`` tipado ``WORKER_SCALED_TO_ZERO`` (não connection-refused).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from deile.orchestration.pipeline import cli_worker_scaler as scaler
from deile.orchestration.pipeline.cli_worker_scaler import (
    EnsureReplicaOutcome, ScaleResult, ensure_replica)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    scaler._reset_cooldown_for_tests()
    # kubectl "existe" por default nos testes (mock dos calls reais).
    monkeypatch.setattr(scaler, "_kubectl_bin", lambda: "kubectl")
    yield
    scaler._reset_cooldown_for_tests()


def _kubectl_seq(*returns):
    """AsyncMock que devolve uma sequência de ``(rc, stdout, stderr)``."""
    return AsyncMock(side_effect=list(returns))


class TestEnsureReplica:
    async def test_core_dispatcher_not_applicable(self):
        for core in ("deile-worker", "claude-worker"):
            out = await ensure_replica(core)
            assert out.result == ScaleResult.NOT_APPLICABLE
            assert out.ok_to_dispatch is True

    async def test_already_scaled_is_ready(self, monkeypatch):
        monkeypatch.setattr(scaler, "_kubectl", _kubectl_seq((0, "1", "")))
        out = await ensure_replica("opencode-worker")
        assert out.result == ScaleResult.READY
        assert out.ok_to_dispatch is True

    async def test_zero_replicas_triggers_scale(self, monkeypatch):
        # 1ª chamada: get → "0"; 2ª: scale → rc 0.
        mock = _kubectl_seq((0, "0", ""), (0, "", ""))
        monkeypatch.setattr(scaler, "_kubectl", mock)
        out = await ensure_replica("opencode-worker")
        assert out.result == ScaleResult.SCALED
        assert out.ok_to_dispatch is True
        # Confirma que o 2º call foi um ``scale ... --replicas=1``.
        scale_call = mock.await_args_list[1].args
        assert "scale" in scale_call
        assert "deployment/opencode-worker" in scale_call
        assert "--replicas=1" in scale_call

    async def test_scale_failure_blocks_dispatch(self, monkeypatch):
        mock = _kubectl_seq((0, "0", ""), (1, "", "forbidden"))
        monkeypatch.setattr(scaler, "_kubectl", mock)
        out = await ensure_replica("opencode-worker")
        assert out.result == ScaleResult.SCALE_FAILED
        assert out.ok_to_dispatch is False
        assert "k8s scale --opencode-worker 1" in out.detail

    async def test_get_failure_blocks_dispatch(self, monkeypatch):
        monkeypatch.setattr(scaler, "_kubectl", _kubectl_seq((1, "", "boom")))
        out = await ensure_replica("opencode-worker")
        assert out.result == ScaleResult.SCALE_FAILED
        assert out.ok_to_dispatch is False

    async def test_no_kubectl_returns_instructive_error(self, monkeypatch):
        monkeypatch.setattr(scaler, "_kubectl_bin", lambda: None)
        out = await ensure_replica("opencode-worker")
        assert out.result == ScaleResult.NO_KUBECTL
        assert out.ok_to_dispatch is False
        assert "k8s scale --opencode-worker 1" in out.detail

    async def test_cooldown_skips_second_scale(self, monkeypatch):
        # 1º ensure: get 0 → scale ok (SCALED, grava cooldown).
        mock = _kubectl_seq((0, "0", ""), (0, "", ""), (0, "0", ""))
        monkeypatch.setattr(scaler, "_kubectl", mock)
        first = await ensure_replica("opencode-worker")
        assert first.result == ScaleResult.SCALED
        # 2º ensure logo em seguida: get 0 mas dentro do cooldown → não re-scale.
        second = await ensure_replica("opencode-worker")
        assert second.result == ScaleResult.COOLDOWN
        assert second.ok_to_dispatch is True
        # Só 3 chamadas kubectl (get, scale, get) — nenhum 2º scale.
        assert mock.await_count == 3


class TestImplementerWiringScaleToZero:
    """O ensure-replica é chamado no caminho de dispatch para CLI workers."""

    async def test_cli_worker_dispatch_scales_before_post(self, monkeypatch):
        from deile.orchestration.pipeline.implementer import WorkerImplementer
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "opencode-worker")
        impl = WorkerImplementer()

        ensure_mock = AsyncMock(
            return_value=EnsureReplicaOutcome(ScaleResult.READY, "ok")
        )
        monkeypatch.setattr(
            "deile.orchestration.pipeline.cli_worker_scaler.ensure_replica",
            ensure_mock,
        )
        with patch.object(
            impl, "_post_dispatch", new_callable=AsyncMock,
        ) as mock_post:
            mock_post.return_value = {"ok": True, "summary": ""}
            await impl._dispatch(
                "brief", channel_id="c", stage="implement",
                branch="auto/issue-1",
            )
        ensure_mock.assert_awaited_once()
        assert ensure_mock.await_args.args[0] == "opencode-worker"
        mock_post.assert_awaited_once()

    async def test_scale_failure_returns_typed_outcome_no_post(self, monkeypatch):
        from deile.orchestration.pipeline.implementer import WorkerImplementer
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "opencode-worker")
        impl = WorkerImplementer()

        ensure_mock = AsyncMock(
            return_value=EnsureReplicaOutcome(
                ScaleResult.SCALE_FAILED,
                "kubectl scale falhou — rode k8s scale --opencode-worker 1",
            )
        )
        monkeypatch.setattr(
            "deile.orchestration.pipeline.cli_worker_scaler.ensure_replica",
            ensure_mock,
        )
        with patch.object(
            impl, "_post_dispatch", new_callable=AsyncMock,
        ) as mock_post:
            outcome = await impl._dispatch(
                "brief", channel_id="c", stage="implement",
                branch="auto/issue-1",
            )
        assert outcome.ok is False
        assert outcome.error.startswith("WORKER_SCALED_TO_ZERO")
        mock_post.assert_not_awaited()

    async def test_core_worker_dispatch_does_not_scale(self, monkeypatch):
        from deile.orchestration.pipeline.implementer import WorkerImplementer
        monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "deile-worker")
        impl = WorkerImplementer()

        ensure_mock = AsyncMock()
        monkeypatch.setattr(
            "deile.orchestration.pipeline.cli_worker_scaler.ensure_replica",
            ensure_mock,
        )
        with patch.object(
            impl, "_post_dispatch", new_callable=AsyncMock,
        ) as mock_post:
            mock_post.return_value = {"ok": True, "summary": ""}
            await impl._dispatch(
                "brief", channel_id="c", stage="implement",
                branch="auto/issue-1",
            )
        # Worker núcleo → ensure_replica NUNCA é chamado.
        ensure_mock.assert_not_awaited()
        mock_post.assert_awaited_once()
