"""Unit tests for the pluggable pipeline implementer strategy.

Covers the factory selection, the Claude strategy (delegates to the injected
``monitor.claude`` + ``monitor.worktrees``) and the deile-worker strategy
(builds the brief, picks the synthetic channel, parses the worker response).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.orchestration.pipeline.claude_dispatcher import ClaudeRunResult
from deile.orchestration.pipeline.github_client import (CommentRef, IssueRef,
                                                        MentionTrigger)
from deile.orchestration.pipeline.implementer import (ClaudeImplementer,
                                                      WorkerImplementer,
                                                      WorkOutcome,
                                                      build_implementer)


def _make_monitor(*, claude_stdout="", claude_rc=0, worktree_raises=False):
    monitor = MagicMock()
    monitor.config = SimpleNamespace(
        repo="owner/name", main_branch="main", base_repo_path=Path("/tmp/fake"),
        mention_handle="@deile-one",
    )
    monitor.branch_for_issue = lambda n: f"auto/issue-{n}"
    if worktree_raises:
        monitor.worktrees.create_branch_worktree = AsyncMock(
            side_effect=RuntimeError("boom")
        )
    else:
        monitor.worktrees.create_branch_worktree = AsyncMock(
            return_value=SimpleNamespace(path=Path("/tmp/fake/.worktrees/x"))
        )
    monitor.claude.run = AsyncMock(
        return_value=ClaudeRunResult(
            returncode=claude_rc, stdout=claude_stdout, stderr="err" if claude_rc else "",
            duration_seconds=0.1, cmd=("claude", "-p", "x"),
        )
    )
    return monitor


def _issue(number=242, title="t", body="b"):
    return SimpleNamespace(number=number, title=title, body=body)


def _pr(number=7, title="t", head_ref="auto/issue-242"):
    return SimpleNamespace(
        number=number, title=title, head_ref=head_ref,
        url=f"https://github.com/owner/name/pull/{number}",
    )


def _comment():
    return SimpleNamespace(
        html_url="https://github.com/owner/name/issues/1#c1",
        body="@deile-one ol\u221a\u00b0", author="someone",
    )


def _mention_trigger_comment(*, trigger_type: str = "comment") -> MentionTrigger:
    """Build a MentionTrigger wrapping a synthetic CommentRef."""
    comment = CommentRef(
        comment_id=1,
        body="@deile-one ola",
        html_url="https://github.com/owner/name/issues/1#issuecomment-1",
        issue_url="https://api.github.com/repos/owner/name/issues/1",
        author="someone",
        kind="issue",
    )
    return MentionTrigger(trigger_type=trigger_type, comment=comment)


def _mention_trigger_assignee_issue(number: int = 100) -> MentionTrigger:
    """Build a MentionTrigger for an assignee on an issue."""
    issue = IssueRef(
        number=number,
        title="test issue",
        url=f"https://github.com/owner/name/issues/{number}",
        labels=(),
    )
    return MentionTrigger(trigger_type="assignee", issue=issue)


# ----- factory ------------------------------------------------------------

class TestFactory:
    """A partir da fase 2 da issue #309, ``build_implementer`` SEMPRE retorna
    :class:`WorkerImplementer` — a decisão de endpoint (``deile-worker`` vs
    ``claude-worker``) é per-stage em runtime via ``dispatch_resolver``. O
    parâmetro ``dispatch_mode`` continua aceito apenas para validar typos
    (fail-fast) e manter compat com chamadas antigas.

    Pré-#309-fase-2: aliases ``claude*`` retornavam :class:`ClaudeImplementer`.
    Agora retornam :class:`WorkerImplementer`. Para construir o legacy
    ClaudeImplementer (CLI local fora do cluster), use
    :func:`get_local_claude_implementer`.
    """

    @pytest.mark.parametrize("mode", ["claude", "claude_code", "claude-code"])
    def test_claude_aliases_return_worker_implementer(self, mode):
        # Mudança semântica de #309 fase 2: aliases ``claude*`` não constroem
        # mais ``ClaudeImplementer``. A escolha de endpoint é runtime via
        # ``dispatch_resolver`` — ``WorkerImplementer`` resolve per-call.
        impl = build_implementer(mode, worker_client=MagicMock())
        assert isinstance(impl, WorkerImplementer)

    @pytest.mark.parametrize("mode", ["deile_worker", "worker", "deile", "deile-worker"])
    def test_worker_aliases(self, mode):
        impl = build_implementer(mode, worker_client=MagicMock())
        assert isinstance(impl, WorkerImplementer)

    def test_unknown_mode_raises(self):
        # Pre-#309-fase-2 esta entrada caía silenciosamente em ClaudeImplementer
        # com logger.warning — um typo em DEILE_PIPELINE_DISPATCH_MODE (ex.:
        # "deile_woker") queimaria ANTHROPIC_API_KEY sem alerta. Fail-fast
        # ValueError surface o erro imediatamente (pilar 03 §6 + dispatch UX).
        with pytest.raises(ValueError, match="unknown pipeline dispatch_mode"):
            build_implementer("nonsense")

    def test_empty_mode_returns_worker_implementer(self):
        # Default vazio/None: WorkerImplementer (resolver runtime decide endpoint).
        impl = build_implementer("")
        assert isinstance(impl, WorkerImplementer)

    def test_none_mode_returns_worker_implementer(self):
        # Sem argumento — mesmo comportamento de vazio.
        impl = build_implementer()
        assert isinstance(impl, WorkerImplementer)

    def test_get_local_claude_implementer_returns_claude(self):
        """Factory exclusiva para uso local fora do cluster (CLI). Continua
        construindo :class:`ClaudeImplementer` (subprocess ``claude -p``)."""
        from deile.orchestration.pipeline.implementer import \
            get_local_claude_implementer
        impl = get_local_claude_implementer()
        assert isinstance(impl, ClaudeImplementer)


# ----- ClaudeImplementer --------------------------------------------------

class TestClaudeImplementer:
    async def test_implement_uses_worktree_and_claude(self):
        monitor = _make_monitor(claude_stdout="https://github.com/owner/name/pull/9")
        out = await ClaudeImplementer().implement(monitor, _issue())
        assert out.ok is True
        assert "pull/9" in out.text
        monitor.worktrees.create_branch_worktree.assert_awaited_once()
        monitor.claude.run.assert_awaited_once()

    async def test_implement_worktree_failure_returns_not_ok(self):
        monitor = _make_monitor(worktree_raises=True)
        out = await ClaudeImplementer().implement(monitor, _issue())
        assert out.ok is False
        assert "worktree" in out.error
        monitor.claude.run.assert_not_awaited()

    async def test_implement_claude_nonzero_returns_not_ok(self):
        monitor = _make_monitor(claude_rc=2)
        out = await ClaudeImplementer().implement(monitor, _issue())
        assert out.ok is False

    async def test_review_uses_worktree_and_claude(self):
        monitor = _make_monitor(claude_stdout="merged https://github.com/owner/name/pull/9")
        out = await ClaudeImplementer().review(monitor, _pr())
        assert out.ok is True
        assert "merged" in out.text.lower()

    async def test_mention_runs_in_base_repo_path(self):
        monitor = _make_monitor(claude_stdout="done")
        trigger = _mention_trigger_comment()
        out = await ClaudeImplementer().mention(
            monitor, trigger,
            trigger_types=["comment"],
            all_triggers=[trigger],
        )
        assert out.ok is True
        _, kwargs = monitor.claude.run.call_args
        assert kwargs["cwd"] == monitor.config.base_repo_path


# ----- WorkerImplementer --------------------------------------------------

class _FakeClient:
    """Captures the dispatch payload and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.last_payload = None
        self.last_wait = None

    async def dispatch(self, payload, *, wait):
        self.last_payload = payload
        self.last_wait = wait
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class TestWorkerImplementer:
    async def test_implement_dispatches_brief_and_parses_ok(self):
        # Issue #373: implement() now dispatches fire-and-forget (nowait=True).
        # The worker returns 202 + task_id; the response has no summary.
        client = _FakeClient({"task_id": "abc123", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.implement(_make_monitor(), _issue(number=242, title="soma", body="impl"))
        assert out.ok is True
        assert out.task_id == "abc123"
        # Fire-and-forget: no summary text in response.
        assert out.text == ""
        assert client.last_payload["channel_id"] == "pipeline-issue-242"
        assert client.last_wait is False
        # Implementation runs under the developer persona.
        assert client.last_payload["persona"] == "developer"
        # The brief must name the repo, the issue number and the branch.
        brief = client.last_payload["brief"]
        assert "owner/name" in brief
        assert "#242" in brief
        assert "auto/issue-242" in brief

    async def test_implement_worker_failure_returns_not_ok(self):
        # Issue #373: fire-and-forget dispatch — transport errors still
        # propagate (the _post_dispatch call itself can fail).
        from deile.infrastructure.deile_worker_client import \
            WorkerDispatchError
        client = _FakeClient(WorkerDispatchError("nope", error_code="WORKER_TIMEOUT"))
        out = await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert out.ok is False
        assert "WORKER_TIMEOUT" in out.error

    async def test_dispatch_error_is_caught(self):
        from deile.infrastructure.deile_worker_client import \
            WorkerDispatchError
        client = _FakeClient(WorkerDispatchError("nope", error_code="WORKER_TIMEOUT"))
        out = await WorkerImplementer(client=client).implement(_make_monitor(), _issue())
        assert out.ok is False
        assert "WORKER_TIMEOUT" in out.error

    async def test_review_uses_pr_channel_and_merged_marker(self):
        # Issue #373: review fresh agora é fire-and-forget (nowait=True), espelhando
        # o implement. O worker retorna 202 + task_id imediatamente; o pipeline
        # reconcilia via ground truth no próximo tick (com resume=True bloqueante).
        client = _FakeClient({"task_id": "rev-abc", "status": "running"})
        out = await WorkerImplementer(client=client).review(_make_monitor(), _pr(number=7))
        assert out.ok is True
        assert out.task_id == "rev-abc"
        # Fire-and-forget: sem summary no response imediato.
        assert out.text == ""
        assert client.last_payload["channel_id"] == "pipeline-pr-7"
        # The review/merge stage is the final quality gate: it runs under the
        # dedicated ``reviewer`` persona, not ``developer``.
        assert client.last_payload["persona"] == "reviewer"
        # Transport-level wait=False confirma fire-and-forget.
        assert client.last_wait is False
        # Após o refactor "PR é o quadro" o brief unificado substitui o brief
        # de QUALITY GATE. Asserts agora cobrem o princípio do brief: descoberta
        # de estado real + checkpoint obrigatório de comentário visível.
        brief = client.last_payload["brief"]
        assert "PASSO 0" in brief
        assert "estado real" in brief.lower() or "ESTADO REAL" in brief

    async def test_review_resume_uses_reviewer_persona(self):
        client = _FakeClient({"ok": True, "summary": "https://github.com/owner/name/pull/7 MERGED"})
        out = await WorkerImplementer(client=client).review(
            _make_monitor(), _pr(number=7), resume=True
        )
        assert out.ok is True
        assert client.last_payload["persona"] == "reviewer"

    async def test_mention_dispatches_to_mention_channel(self):
        client = _FakeClient({"ok": True, "summary": "respondido"})
        trigger = _mention_trigger_comment()
        out = await WorkerImplementer(client=client).mention(
            _make_monitor(), trigger,
            trigger_types=["comment"],
            all_triggers=[trigger],
        )
        assert out.ok is True
        assert client.last_payload["channel_id"] == "pipeline-mention-issue-1"
        # Mentions run under the developer persona (only PR review uses reviewer).
        assert client.last_payload["persona"] == "developer"


class _HangingClient:
    """Cliente cujo ``dispatch`` nunca retorna — simula o worker pendurado.

    Reproduz a regressão de produção 2026-06-01: o dispatch ``wait=True``
    bloqueava o tick indefinidamente quando o worker aceitava a conexão mas
    não respondia. ``started`` permite asserir que a chamada de fato começou.
    """

    def __init__(self):
        self.started = asyncio.Event()
        self.last_wait = None

    async def dispatch(self, payload, *, wait, endpoint_url=None):
        self.last_wait = wait
        self.started.set()
        # Espera "para sempre" — o watchdog do tick deve interromper.
        await asyncio.Event().wait()


class TestTickWatchdog:
    """Regressão: um dispatch ``wait=True`` pendurado NUNCA congela o tick.

    O watchdog em :meth:`WorkerImplementer._post_dispatch` envolve a chamada
    HTTP bloqueante num ``asyncio.wait_for``; ao estourar, converte o hang num
    :class:`WorkerDispatchError` recuperável (``WORKER_TICK_WATCHDOG``) que o
    stage handler trata como falha do alvo — o tick prossegue.
    """

    async def test_hung_dispatch_does_not_freeze_the_tick(self, monkeypatch):
        import deile.orchestration.pipeline.implementer as impl_mod

        # Encolhe o budget HTTP (lido via import local em _post_dispatch) e o
        # buffer do watchdog para o teste rodar em milissegundos em vez de 2h.
        monkeypatch.setattr(
            "deile.infrastructure.deile_worker_client.MAX_DISPATCH_BUDGET_S",
            0.05,
        )
        monkeypatch.setattr(
            impl_mod.WorkerImplementer, "_TICK_WATCHDOG_BUFFER_S", 0.05,
        )

        client = _HangingClient()
        impl = WorkerImplementer(client=client)

        # review(resume=True) despacha com wait=True (caminho bloqueante).
        # review(resume=False) agora é nowait — não trava o tick por design.
        out = await asyncio.wait_for(
            impl.review(_make_monitor(), _pr(number=7), resume=True), timeout=5.0,
        )

        assert client.started.is_set()
        assert client.last_wait is True
        # Hang convertido em falha recuperável — NÃO uma exceção que derruba o tick.
        assert out.ok is False
        assert "WORKER_TICK_WATCHDOG" in out.error

    async def test_watchdog_reraises_cancellation(self, monkeypatch):
        """``asyncio.CancelledError`` (shutdown do monitor) é re-levantado,
        nunca silenciado pelo watchdog."""
        import deile.orchestration.pipeline.implementer as impl_mod

        monkeypatch.setattr(
            "deile.infrastructure.deile_worker_client.MAX_DISPATCH_BUDGET_S",
            3600.0,
        )
        monkeypatch.setattr(
            impl_mod.WorkerImplementer, "_TICK_WATCHDOG_BUFFER_S", 0.0,
        )

        client = _HangingClient()
        impl = WorkerImplementer(client=client)

        task = asyncio.ensure_future(impl.review(_make_monitor(), _pr(number=7)))
        await client.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestWorkOutcome:
    def test_defaults(self):
        o = WorkOutcome(ok=True, text="x")
        assert o.error == ""


# ---------------------------------------------------------------------------
# Testes de nowait para critique / refine / review (issue #373 extensão)
# ---------------------------------------------------------------------------

def _make_monitor_with_forge():
    """Mesmo que _make_monitor(), mas com forge.config real (necessário para critique/refine)."""
    monitor = _make_monitor()
    from deile.orchestration.forge.base import ForgeConfig, ForgeKind
    monitor.forge = SimpleNamespace(
        config=ForgeConfig(
            kind=ForgeKind.GITHUB,
            host="github.com",
            project_path="owner/name",
            cli_path="/usr/bin/gh",
        ),
    )
    return monitor


def _issue_with_labels(number=10, labels=("intent",)):
    return SimpleNamespace(number=number, title="Issue teste", body="corpo da issue", labels=labels)


class TestCritiqueRefineNowait:
    """critique() e refine() devem ser fire-and-forget (nowait=True) com ledger_key."""

    async def test_critique_is_nowait_and_returns_task_id(self):
        client = _FakeClient({"task_id": "crit-t1", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.critique(_make_monitor_with_forge(), _issue_with_labels(number=10))
        # Fire-and-forget: retorna task_id sem bloquear.
        assert out.ok is True
        assert out.task_id == "crit-t1"
        assert out.text == ""
        # Transport-level wait=False confirma nowait.
        assert client.last_wait is False

    async def test_critique_payload_has_wait_for_result_false(self):
        client = _FakeClient({"task_id": "crit-t2", "status": "running"})
        impl = WorkerImplementer(client=client)
        await impl.critique(_make_monitor_with_forge(), _issue_with_labels(number=10))
        assert client.last_payload["wait_for_result"] is False

    async def test_critique_gravar_ledger_com_task_id(self):
        """critique() com nowait=True deve gravar task_id no DispatchLedger."""
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
        client = _FakeClient({"task_id": "crit-t3", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.critique(_make_monitor_with_forge(), _issue_with_labels(number=42))
        assert out.task_id == "crit-t3"
        # Ledger deve ter a entry para a issue.
        record = impl._ledger.get(DispatchLedger.key_for_issue(42))
        assert record is not None
        assert record["task_id"] == "crit-t3"

    async def test_refine_is_nowait_and_returns_task_id(self):
        client = _FakeClient({"task_id": "ref-t1", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.refine(_make_monitor_with_forge(), _issue_with_labels(number=11))
        assert out.ok is True
        assert out.task_id == "ref-t1"
        assert out.text == ""
        assert client.last_wait is False

    async def test_refine_payload_has_wait_for_result_false(self):
        client = _FakeClient({"task_id": "ref-t2", "status": "running"})
        impl = WorkerImplementer(client=client)
        await impl.refine(_make_monitor_with_forge(), _issue_with_labels(number=11))
        assert client.last_payload["wait_for_result"] is False

    async def test_refine_grava_ledger_com_task_id(self):
        """refine() com nowait=True deve gravar task_id no DispatchLedger."""
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
        client = _FakeClient({"task_id": "ref-t3", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.refine(_make_monitor_with_forge(), _issue_with_labels(number=55))
        assert out.task_id == "ref-t3"
        record = impl._ledger.get(DispatchLedger.key_for_issue(55))
        assert record is not None
        assert record["task_id"] == "ref-t3"

    async def test_critique_usa_stage_classify_e_persona(self):
        """critique() roteia pelo stage='classify' (knob próprio, distinto do
        refine) e mantém a persona do tipo da issue. São duas chamadas LLM
        separadas — a crítica julga CLARO/VAGO; o refine reescreve."""
        client = _FakeClient({"task_id": "crit-t4", "status": "running"})
        impl = WorkerImplementer(client=client)
        await impl.critique(_make_monitor_with_forge(), _issue_with_labels(number=10, labels=("intent",)))
        assert client.last_payload["stage"] == "classify"
        # 'intent' → persona 'analyst'
        assert client.last_payload["persona"] == "analyst"

    async def test_refine_stage_e_persona_preservados(self):
        """refine() deve continuar usando stage='refine' e persona da issue."""
        client = _FakeClient({"task_id": "ref-t4", "status": "running"})
        impl = WorkerImplementer(client=client)
        await impl.refine(_make_monitor_with_forge(), _issue_with_labels(number=10, labels=("feature",)))
        assert client.last_payload["stage"] == "refine"
        # 'feature' → persona 'architect'
        assert client.last_payload["persona"] == "architect"

    async def test_review_fresh_e_nowait(self):
        """review(resume=False) deve ser nowait=True (fire-and-forget)."""
        client = _FakeClient({"task_id": "rev-t1", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.review(_make_monitor(), _pr(number=5))
        assert out.ok is True
        assert out.task_id == "rev-t1"
        assert out.text == ""
        assert client.last_wait is False

    async def test_review_resume_permanece_bloqueante(self):
        """review(resume=True) deve continuar bloqueante (wait=True)."""
        client = _FakeClient({"ok": True, "summary": "https://github.com/owner/name/pull/5 MERGED"})
        impl = WorkerImplementer(client=client)
        out = await impl.review(_make_monitor(), _pr(number=5), resume=True)
        assert out.ok is True
        assert client.last_wait is True

    async def test_review_fresh_grava_ledger(self):
        """review fresh deve gravar task_id no DispatchLedger (caminho nowait)."""
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
        client = _FakeClient({"task_id": "rev-t2", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.review(_make_monitor(), _pr(number=77))
        assert out.task_id == "rev-t2"
        record = impl._ledger.get(DispatchLedger.key_for_pr(77))
        assert record is not None
        assert record["task_id"] == "rev-t2"

    async def test_review_resume_preservado_sem_nowait(self):
        """review(resume=True) NÃO deve passar nowait — caminho bloqueante intacto."""
        client = _FakeClient({"ok": True, "summary": ""})
        impl = WorkerImplementer(client=client)
        out = await impl.review(_make_monitor(), _pr(number=88), resume=True)
        # Caminho wait=True: retorna summary do response, não task_id nowait.
        assert out.ok is True
        assert client.last_wait is True


# ---------------------------------------------------------------------------
# Fix #8 (issue #521) — WorkerImplementer.address_review
# ---------------------------------------------------------------------------

class TestAddressReviewDispatch:
    """address_review() deve despachar fire-and-forget com stage=implement e
    persona=developer — NÃO usa stage=pr_review como a review normal.

    O brief enviado instrui o worker a LER a última review REQUEST_CHANGES e
    APLICAR o fix + push; nunca revisar, comentar veredito ou mergear.
    """

    async def test_address_review_e_nowait(self):
        """address_review() deve ser fire-and-forget (nowait=True)."""
        client = _FakeClient({"task_id": "addr-t1", "status": "running"})
        impl = WorkerImplementer(client=client)
        out = await impl.address_review(_make_monitor_with_forge(), _pr(number=42))
        assert out.ok is True
        assert out.task_id == "addr-t1"
        assert client.last_wait is False

    async def test_address_review_usa_stage_implement(self):
        """address_review() usa stage='implement', não 'pr_review'."""
        client = _FakeClient({"task_id": "addr-t2", "status": "running"})
        impl = WorkerImplementer(client=client)
        await impl.address_review(_make_monitor_with_forge(), _pr(number=42))
        assert client.last_payload["stage"] == "implement"

    async def test_address_review_usa_persona_developer(self):
        """address_review() usa persona='developer' (implement, não review)."""
        client = _FakeClient({"task_id": "addr-t3", "status": "running"})
        impl = WorkerImplementer(client=client)
        await impl.address_review(_make_monitor_with_forge(), _pr(number=42))
        assert client.last_payload["persona"] == "developer"

    async def test_address_review_brief_instrui_aplique(self):
        """O brief de address deve conter 'APLIQUE' e proibir revisar/mergear."""
        client = _FakeClient({"task_id": "addr-t4", "status": "running"})
        impl = WorkerImplementer(client=client)
        await impl.address_review(_make_monitor_with_forge(), _pr(number=42))
        brief = client.last_payload.get("brief", "")
        assert "APLIQUE" in brief
        assert "NÃO revise" in brief
        assert "NÃO mergeie" in brief
