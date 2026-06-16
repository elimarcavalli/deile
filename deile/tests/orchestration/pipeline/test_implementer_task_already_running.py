"""Testes do tratamento de TASK_ALREADY_RUNNING (409) no WorkerImplementer.

Cobre o mecanismo de lease do claude-worker: quando o worker retorna 409
com error_code=TASK_ALREADY_RUNNING (workspace com lease ativo em outro pod),
o implementer deve:
  1. Retornar WorkOutcome(ok=False) com mensagem indicando skip neste tick.
  2. NÃO limpar o DispatchLedger (a task está em andamento, não falhou).

Esses dois comportamentos garantem que o pipeline trate o 409 do lease da
mesma forma que já trata o CONCURRENT_DISPATCH_BLOCKED: aguardar o próximo
tick para nova tentativa, sem multiplicar Opus na mesma issue.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.infrastructure.deile_worker_client import WorkerDispatchError
from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
from deile.orchestration.pipeline.implementer import WorkerImplementer, WorkOutcome

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker_409_client(error_code: str = "TASK_ALREADY_RUNNING") -> MagicMock:
    """Cliente que sempre levanta WorkerDispatchError 409 com o error_code dado."""
    client = MagicMock()
    exc = WorkerDispatchError(
        message=f"workspace ocupado ({error_code})",
        error_code=error_code,
    )
    client.dispatch = AsyncMock(side_effect=exc)
    client.get_resume_info = AsyncMock(return_value=None)
    return client


def _make_monitor() -> MagicMock:
    monitor = MagicMock()
    monitor.config = SimpleNamespace(
        repo="owner/repo",
        main_branch="main",
        base_repo_path=Path("/tmp/fake"),
        mention_handle="@deile-one",
    )
    monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
    monitor.forge = MagicMock()
    monitor.forge.config = MagicMock()
    return monitor


def _make_ledger(tmp_path: Path) -> DispatchLedger:
    return DispatchLedger(path=tmp_path / "dispatches.json")


def _issue(number: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        title="Test issue",
        body="body",
        labels=(),
    )


# ---------------------------------------------------------------------------
# Caso 13 — 409 TASK_ALREADY_RUNNING é tratado como _still_alive
# ---------------------------------------------------------------------------


class TestImplementerTaskAlreadyRunning:
    @pytest.mark.unit
    async def test_implementer_treats_409_as_still_alive(self, tmp_path: Path):
        """Quando o worker retorna 409 TASK_ALREADY_RUNNING, o WorkerImplementer
        deve retornar WorkOutcome(ok=False) com erro indicando skip neste tick,
        sem escalar como falha real da task.

        Comportamento equivalente ao CONCURRENT_DISPATCH_BLOCKED (que já existe)
        e ao _still_alive do _resolve_resume_meta — a task está em andamento
        em outro pod; o pipeline aguarda o próximo tick.
        """
        ledger = _make_ledger(tmp_path)
        client = _make_worker_409_client("TASK_ALREADY_RUNNING")
        impl = WorkerImplementer(client=client, ledger=ledger)

        monitor = _make_monitor()
        issue = _issue(42)

        # Patcha resolve_stage_model para não precisar de env vars.
        with (
            patch(
                "deile.orchestration.pipeline.implementer.resolve_stage_model",
                return_value=None,
            ),
            patch(
                "deile.orchestration.pipeline.implementer.get_endpoint_for",
                return_value="http://claude-worker:8767",
            ),
            patch(
                "deile.orchestration.pipeline.implementer.resolve_stage_dispatcher",
                return_value="claude-worker",
            ),
        ):
            outcome = await impl.implement(monitor, issue, resume=False)

        assert isinstance(outcome, WorkOutcome)
        assert outcome.ok is False
        # A mensagem de erro deve conter indicativo de skip (não é falha real).
        assert (
            "LEASE" in outcome.error or "skip" in outcome.error.lower()
        ), f"erro esperado indicar skip via lease, mas foi: {outcome.error!r}"

    @pytest.mark.unit
    async def test_implementer_concurrent_dispatch_blocked_still_works(
        self, tmp_path: Path
    ):
        """Regressão: CONCURRENT_DISPATCH_BLOCKED (error_code preexistente)
        ainda funciona corretamente após adição do TASK_ALREADY_RUNNING."""
        ledger = _make_ledger(tmp_path)
        client = _make_worker_409_client("CONCURRENT_DISPATCH_BLOCKED")
        impl = WorkerImplementer(client=client, ledger=ledger)

        monitor = _make_monitor()
        issue = _issue(43)

        with (
            patch(
                "deile.orchestration.pipeline.implementer.resolve_stage_model",
                return_value=None,
            ),
            patch(
                "deile.orchestration.pipeline.implementer.get_endpoint_for",
                return_value="http://claude-worker:8767",
            ),
            patch(
                "deile.orchestration.pipeline.implementer.resolve_stage_dispatcher",
                return_value="claude-worker",
            ),
        ):
            outcome = await impl.implement(monitor, issue, resume=False)

        assert outcome.ok is False
        assert "CONCURRENT" in outcome.error or "skip" in outcome.error.lower()


# ---------------------------------------------------------------------------
# Caso 14 — Ledger NÃO é limpo após 409
# ---------------------------------------------------------------------------


class TestLedgerNotClearedOn409:
    @pytest.mark.unit
    async def test_implementer_ledger_not_cleared_on_409(self, tmp_path: Path):
        """Após 409 TASK_ALREADY_RUNNING, a entrada no DispatchLedger deve
        ser preservada para que o próximo dispatch possa retomar via resume.

        O 409 significa que a task ESTÁ RODANDO — limpar o ledger seria
        descartarmos a referência de resume, forçando um fresh dispatch
        (clone + análise repetida) desnecessário.
        """
        ledger = _make_ledger(tmp_path)
        # Registra uma entrada preexistente no ledger (simula dispatch anterior).
        ledger.record(
            DispatchLedger.key_for_issue(42),
            task_id="abcd1234abcd1234",
            session_id="sess-abcd",
            stage="implement",
            branch="auto/issue-42",
            worker_kind="claude",
        )
        assert ledger.get(DispatchLedger.key_for_issue(42)) is not None

        client = _make_worker_409_client("TASK_ALREADY_RUNNING")
        impl = WorkerImplementer(client=client, ledger=ledger)
        monitor = _make_monitor()
        issue = _issue(42)

        with (
            patch(
                "deile.orchestration.pipeline.implementer.resolve_stage_model",
                return_value=None,
            ),
            patch(
                "deile.orchestration.pipeline.implementer.get_endpoint_for",
                return_value="http://claude-worker:8767",
            ),
            patch(
                "deile.orchestration.pipeline.implementer.resolve_stage_dispatcher",
                return_value="claude-worker",
            ),
            # Simula _resolve_resume_meta retornando None (sem meta do worker)
            # para garantir que o 409 vem do dispatch, não do resume-info.
            patch.object(
                impl, "_resolve_resume_meta", new=AsyncMock(return_value=None)
            ),
        ):
            outcome = await impl.implement(monitor, issue, resume=False)

        assert outcome.ok is False
        # LEDGER DEVE ESTAR INTACTO após 409.
        remaining = ledger.get(DispatchLedger.key_for_issue(42))
        assert (
            remaining is not None
        ), "ledger não deve ser limpo após 409 — a task está em andamento"
        assert remaining.get("task_id") == "abcd1234abcd1234"
