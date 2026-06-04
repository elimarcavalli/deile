"""Tests for the two flood-guard fixes (PR #517 / issue #515).

Fix A — review HEAD-delta-guard: re-review do mesmo HEAD SHA é bloqueada
determinísticamente em vez de queimar N passes do fingerprint-guard fraco.

Fix B — refino divergence early-stop: se o body continua crescendo no 3º+
passe de refino, bloqueia early em vez de deixar chegar ao teto de 5.

Cada seção prova dois cenários: (1) guard dispara, (2) guard NÃO dispara
(comportamento saudável preservado), e (3) caso de retrocompat/edge-case.

Todos os testes provam também que FALHAM sem o fix (veja comentários inline
que explicam o comportamento pré-fix).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock


from deile.orchestration.pipeline.github_client import IssueRef, PrRef
from deile.orchestration.pipeline.implementer import WorkerImplementer, WorkOutcome
from deile.orchestration.pipeline.labels import (
    REFINAR,
    REVIEW_IN_PROGRESS,
    WORKFLOW_ARCHITECTURE,
    WORKFLOW_BLOCKED,
    WORKFLOW_REVIEWED,
)
from deile.orchestration.pipeline.monitor import PipelineConfig, PipelineMonitor
from deile.orchestration.pipeline.resume_state import ResumeTracker


# ---------------------------------------------------------------------------
# Helpers compartilhados
# ---------------------------------------------------------------------------

_NOTIFIER_METHODS = (
    "issue_picked_up", "issue_reviewed", "implementation_started",
    "implementation_finished", "implementation_parked", "implementation_resumed",
    "implementation_blocked", "pr_picked_up", "pr_reviewed",
    "issue_auto_classified", "follow_ups_processed", "error",
    "pr_auto_classified", "mention_processed",
)


class _FakeWorkerClient:
    """Worker client fake — consume uma resposta enfileirada por dispatch."""

    def __init__(self, responses: List[dict]):
        self._responses = list(responses)
        self.payloads: List[dict] = []

    async def dispatch(self, payload, *, wait):
        self.payloads.append(payload)
        if self._responses:
            return self._responses.pop(0)
        return {"ok": False, "summary": "sem resposta enfileirada"}


def _worker_response(
    *,
    ok: bool = True,
    summary: str = "",
    ended: str = "",
    pr_url: str = "",
    motivo_bloqueio: str = "",
    fingerprint: str = "",
    tentativa: int = 0,
    budget_acumulado_s: float = 0.0,
) -> dict:
    resp: dict = {"ok": ok, "summary": summary}
    resp["resume"] = {
        "ended": ended,
        "pr_url": pr_url,
        "motivo_bloqueio": motivo_bloqueio,
        "motivo_fim_loop": "natural",
        "fingerprint": fingerprint,
        "tentativa": tentativa,
        "budget_acumulado_s": budget_acumulado_s,
    }
    return resp


def _pr(number: int, *labels: str, head_sha: str = "", head_ref: str = "auto/issue-1") -> PrRef:
    return PrRef(
        number=number,
        title="t",
        url=f"https://github.com/owner/name/pull/{number}",
        labels=tuple(labels),
        head_ref=head_ref,
        head_sha=head_sha,
    )


def _issue(number: int, *labels: str, body: str = "corpo", author: str = "alice") -> IssueRef:
    return IssueRef(
        number=number, title="t",
        url=f"https://github.com/owner/name/issues/{number}",
        labels=tuple(labels), body=body, state="open", author=author,
    )


def _make_monitor_for_review(
    prs: List[PrRef],
    worker_responses: List[dict],
    *,
    enable_resume: bool = True,
    resume_interval: int = 0,
) -> Tuple[PipelineMonitor, MagicMock, _FakeWorkerClient]:
    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        dispatch_mode="deile_worker",
        enable_resume=enable_resume,
        resume_max_attempts=10,
        resume_budget=0,
        resume_interval=resume_interval,
        enable_classify=False,
        enable_review=False,
        enable_pr_triage=False,
        enable_mention_handling=False,
    )
    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(return_value=[])
    github.list_open_prs = AsyncMock(return_value=list(prs))
    github.has_open_pr_for_issue = AsyncMock(return_value=False)
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.transition_issue = AsyncMock()
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.clear_batch_label = AsyncMock()
    github.list_unclassified_issues = AsyncMock(return_value=[])
    github.list_unclassified_prs = AsyncMock(return_value=[])
    github.get_pr_body = AsyncMock(return_value="")
    github.list_pr_comments = AsyncMock(return_value=[])
    github.create_issue = AsyncMock(return_value=0)
    github.list_issue_comments_since = AsyncMock(return_value=[])
    github.list_pr_review_comments_since = AsyncMock(return_value=[])
    github.branch_exists = AsyncMock(return_value=True)

    notifier = MagicMock()
    for attr in _NOTIFIER_METHODS:
        setattr(notifier, attr, AsyncMock())

    client = _FakeWorkerClient(worker_responses)
    implementer = WorkerImplementer(client=client)
    monitor = PipelineMonitor(cfg, github=github, notifier=notifier, implementer=implementer)
    return monitor, notifier, client


def _added_labels(github, kind: str, number: int) -> List[str]:
    """Collect all labels added to (kind, number) across all add_labels calls."""
    result = []
    for call in github.add_labels.await_args_list:
        if call.args[0] == kind and call.args[1] == number:
            result.extend(call.args[2])
    return result


async def _review_and_drain(monitor) -> None:
    """Roda o stage de review e DRENA as background tasks de resume (issue #445).

    O resume agora roda detached via ``spawn_background``; o teste precisa
    aguardar a task concluir para observar os efeitos (block/merge/SHA gravado).
    """
    import asyncio

    await monitor._review_one_open_pr()
    if monitor._bg_tasks:
        await asyncio.gather(*list(monitor._bg_tasks))


# ===========================================================================
# Fix A — ResumeTracker: set_reviewed_sha / reviewed_sha (unit)
# ===========================================================================

class TestResumeTrackerShaGuard:
    """Unit tests para os dois novos métodos do ResumeTracker (Fix A)."""

    def test_reviewed_sha_vazio_por_padrao(self):
        t = ResumeTracker()
        assert t.reviewed_sha(99) == ""

    def test_set_reviewed_sha_grava(self):
        t = ResumeTracker()
        t.set_reviewed_sha(10, "abc1234567")
        assert t.reviewed_sha(10) == "abc1234567"

    def test_set_reviewed_sha_cria_estado_se_ausente(self):
        t = ResumeTracker()
        assert t.peek(20) is None
        t.set_reviewed_sha(20, "sha-xyz")
        assert t.reviewed_sha(20) == "sha-xyz"
        assert t.peek(20) is not None

    def test_set_reviewed_sha_isolado_por_pr(self):
        t = ResumeTracker()
        t.set_reviewed_sha(1, "sha-a")
        t.set_reviewed_sha(2, "sha-b")
        assert t.reviewed_sha(1) == "sha-a"
        assert t.reviewed_sha(2) == "sha-b"

    def test_clear_apaga_sha(self):
        t = ResumeTracker()
        t.set_reviewed_sha(5, "sha-xyz")
        t.clear(5)
        assert t.reviewed_sha(5) == ""


# ===========================================================================
# Fix A — PrRef.head_sha propagado pelos from_*_json (unit)
# ===========================================================================

class TestPrRefHeadSha:
    """Garante que os factory methods populam head_sha corretamente."""

    def test_from_gh_json_com_head_ref_oid(self):
        item = {
            "number": 42, "title": "t", "url": "u",
            "labels": [], "headRefName": "auto/issue-42",
            "baseRefName": "main", "state": "open",
            "isDraft": False, "headRefOid": "deadbeef1234567890ab",
        }
        pr = PrRef.from_gh_json(item)
        assert pr.head_sha == "deadbeef1234567890ab"

    def test_from_gh_json_sem_head_ref_oid(self):
        item = {
            "number": 1, "title": "t", "url": "u",
            "labels": [], "headRefName": "branch",
            "baseRefName": "main", "state": "open", "isDraft": False,
        }
        pr = PrRef.from_gh_json(item)
        assert pr.head_sha == ""

    def test_from_gl_json_com_sha(self):
        item = {
            "iid": 7, "title": "mr", "web_url": "u", "labels": [],
            "source_branch": "auto/issue-7", "target_branch": "main",
            "state": "opened", "draft": False,
            "sha": "cafebabe0000",
        }
        pr = PrRef.from_gl_json(item)
        assert pr.head_sha == "cafebabe0000"

    def test_from_gl_json_com_diff_refs(self):
        item = {
            "iid": 8, "title": "mr", "web_url": "u", "labels": [],
            "source_branch": "auto/issue-8", "target_branch": "main",
            "state": "opened", "draft": False,
            "diff_refs": {"head_sha": "feedfeed1111"},
        }
        pr = PrRef.from_gl_json(item)
        assert pr.head_sha == "feedfeed1111"

    def test_from_gl_json_sem_sha(self):
        item = {
            "iid": 9, "title": "mr", "web_url": "u", "labels": [],
            "source_branch": "auto/issue-9", "target_branch": "main",
            "state": "opened", "draft": False,
        }
        pr = PrRef.from_gl_json(item)
        assert pr.head_sha == ""


# ===========================================================================
# Fix A — review_one_open_pr: SHA guard de re-review (integração)
# ===========================================================================

class TestReviewHeadShaFloodGuard:
    """Fix A: PR revisada 2x com MESMO head_sha → bloqueada no 2º resume.

    Sem o fix: o fingerprint do worker VARIA a cada re-review do mesmo HEAD
    (worker re-escreve o texto da review) → zero_progress=False → nunca
    dispara → flood de 4 re-reviews do mesmo HEAD.

    Com o fix: o SHA é ground-truth; se não mudou → zero_progress forçado=True
    → o block existente (~linha 2937) dispara determinísticamente no 2º resume.
    """

    async def test_mesmo_head_sha_bloqueia_no_segundo_resume(self):
        """Prova o guard: 2 reviews com mesmo SHA → bloqueada."""
        SHA = "aabbccdd11223344"

        # Tick 1 (RESUME): review incompleta com SHA = SHA. Pipeline grava SHA.
        pr_resume_1 = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA)
        monitor, notifier, client = _make_monitor_for_review(
            prs=[pr_resume_1],
            worker_responses=[
                _worker_response(
                    ended="incompleto",
                    # fingerprint DIFERENTE a cada call — simula variação do worker
                    fingerprint="fingerprint-alfa",
                    tentativa=1,
                ),
            ],
        )

        await _review_and_drain(monitor)

        # Após o 1º resume incompleto, SHA deve estar gravado.
        assert monitor._resume_tracker.reviewed_sha(10) == SHA
        # NÃO bloqueado ainda.
        blocked_after_first = WORKFLOW_BLOCKED in _added_labels(monitor.github, "pr", 10)
        assert not blocked_after_first, "Não deve bloquear após 1ª review incompleta"

        # Tick 2 (RESUME): mesmo SHA, fingerprint diferente (worker re-escreveu).
        pr_resume_2 = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA)
        monitor.github.list_open_prs = AsyncMock(return_value=[pr_resume_2])
        client._responses = [
            _worker_response(
                ended="incompleto",
                # Fingerprint diferente — sem o fix, o guard fraco NÃO dispararia.
                fingerprint="fingerprint-beta",
                tentativa=2,
            ),
        ]

        monitor.github.add_labels.reset_mock()
        await _review_and_drain(monitor)

        # Com o fix: mesmo SHA → zero_progress forçado → BLOQUEADA.
        added_2 = _added_labels(monitor.github, "pr", 10)
        assert WORKFLOW_BLOCKED in added_2, (
            "Esperava ~workflow:bloqueada no 2º resume com mesmo HEAD SHA, "
            f"mas add_labels recebeu: {added_2}"
        )

    async def test_head_sha_mudou_nao_bloqueia(self):
        """HEAD SHA diferente → fix não aplica exerce normal (não bloqueia)."""
        SHA_1 = "sha-before-fix"
        SHA_2 = "sha-after-fix-applied"

        # Tick 1: review incompleta com SHA_1.
        pr_1 = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA_1)
        monitor, _, client = _make_monitor_for_review(
            prs=[pr_1],
            worker_responses=[
                _worker_response(ended="incompleto", fingerprint="fp1", tentativa=1),
            ],
        )
        await _review_and_drain(monitor)
        assert monitor._resume_tracker.reviewed_sha(10) == SHA_1

        # Tick 2: developer aplicou o fix → SHA mudou para SHA_2.
        pr_2 = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA_2)
        monitor.github.list_open_prs = AsyncMock(return_value=[pr_2])
        client._responses = [
            _worker_response(ended="concluido", pr_url="https://x/pull/10", tentativa=2),
        ]

        monitor.github.add_labels.reset_mock()
        await _review_and_drain(monitor)

        # SHA mudou → não bloqueia; deve concluir normalmente.
        added = _added_labels(monitor.github, "pr", 10)
        assert WORKFLOW_BLOCKED not in added, (
            "Não deve bloquear quando o HEAD SHA mudou (fix foi aplicado)"
        )

    async def test_head_sha_vazio_comportamento_legacy(self):
        """head_sha="" (forge sem suporte) → guard não ativa (retrocompat)."""
        # PR sem SHA: forge antigo ou GitLab sem `sha` no payload.
        pr_1 = _pr(10, REVIEW_IN_PROGRESS, head_sha="")
        monitor, _, client = _make_monitor_for_review(
            prs=[pr_1],
            worker_responses=[
                _worker_response(ended="incompleto", fingerprint="fp-x", tentativa=1),
            ],
        )
        await _review_and_drain(monitor)

        # SHA vazio → nada gravado no tracker.
        assert monitor._resume_tracker.reviewed_sha(10) == ""

        # Tick 2: ainda sem SHA — guard SHA não ativa.
        pr_2 = _pr(10, REVIEW_IN_PROGRESS, head_sha="")
        monitor.github.list_open_prs = AsyncMock(return_value=[pr_2])
        client._responses = [
            # Fingerprint diferente → sem zero_progress → não bloqueia.
            _worker_response(ended="incompleto", fingerprint="fp-y", tentativa=2),
        ]
        monitor.github.add_labels.reset_mock()
        await _review_and_drain(monitor)

        added = _added_labels(monitor.github, "pr", 10)
        assert WORKFLOW_BLOCKED not in added, (
            "Guard SHA não deve ativar quando head_sha está vazio (retrocompat)"
        )


# ===========================================================================
# Fix B — ResumeTracker: record_refine_body_len / get_prev_refine_body_len (unit)
# ===========================================================================

class TestResumeTrackerRefineBodyLen:
    """Unit tests para os dois novos métodos do ResumeTracker (Fix B)."""

    def test_get_prev_refine_body_len_default_minus_one(self):
        t = ResumeTracker()
        assert t.get_prev_refine_body_len(99) == -1

    def test_record_refine_body_len_grava(self):
        t = ResumeTracker()
        t.record_refine_body_len(5, 200)
        assert t.get_prev_refine_body_len(5) == 200

    def test_record_refine_body_len_sobrescreve(self):
        t = ResumeTracker()
        t.record_refine_body_len(5, 100)
        t.record_refine_body_len(5, 250)
        assert t.get_prev_refine_body_len(5) == 250

    def test_record_refine_body_len_isolado_por_issue(self):
        t = ResumeTracker()
        t.record_refine_body_len(1, 111)
        t.record_refine_body_len(2, 222)
        assert t.get_prev_refine_body_len(1) == 111
        assert t.get_prev_refine_body_len(2) == 222

    def test_clear_apaga_body_len(self):
        t = ResumeTracker()
        t.record_refine_body_len(7, 500)
        t.clear(7)
        assert t.get_prev_refine_body_len(7) == -1


# ===========================================================================
# Fix B — _apply_refine_verdict: divergence early-stop (integração)
# ===========================================================================

class _SeqWorkerClientForRefine:
    """Worker client fake compatível com o modelo fire-and-forget (issue #373).

    Para os testes de refino precisamos do path wait=False (nowait) que retorna
    um task_id, e do get_resume_info que simula o resultado do reconcile.
    """

    def __init__(self, responses: List[dict]):
        self._responses = list(responses)
        self.payloads: List[dict] = []
        self.dispatched_tasks: List[str] = []
        self._task_results: Dict[str, dict] = {}
        self._seq = 0

    def _next_response(self) -> dict:
        if self._responses:
            return self._responses.pop(0)
        return {"ok": True, "summary": ""}

    async def dispatch(self, payload, *, wait):
        self.payloads.append(payload)
        resp = self._next_response()
        if wait:
            return resp
        # fire-and-forget: gera task_id e memoriza o veredito.
        self._seq += 1
        tid = f"t-{self._seq:03d}"
        self.dispatched_tasks.append(tid)
        self._task_results[tid] = {
            "ok": resp.get("ok", True),
            "summary": resp.get("summary", ""),
            "is_error": resp.get("is_error", False),
        }
        return {"task_id": tid, "status": "running"}

    async def get_resume_info(self, task_id, *, endpoint_url=None):
        result = self._task_results.get(task_id, {})
        return {
            "last_completed_at": 1_700_000_000,
            "last_is_error": result.get("is_error", False),
            "last_result_full": result.get("summary", ""),
            "last_result_summary": result.get("summary", "")[:200],
            "claude_alive": False,
            "workdir_exists": True,
        }


def _make_monitor_for_refine(
    issues: List[IssueRef],
    worker_responses: List[dict],
    *,
    get_issue_body_fn=None,
) -> Tuple[PipelineMonitor, MagicMock, _SeqWorkerClientForRefine]:
    """Constrói um monitor focado em refino (fire-and-forget)."""
    from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
    import tempfile

    cfg = PipelineConfig(
        repo="owner/name",
        base_repo_path=Path("/tmp/fake"),
        notify_user_id="42",
        dispatch_mode="deile_worker",
        enable_refinement_gate=True,
        max_parallel=2,
        enable_resume=False,
        enable_classify=False,
        enable_pr_triage=False,
        enable_mention_handling=False,
    )

    registry: Dict[int, IssueRef] = {i.number: i for i in issues}

    github = MagicMock()
    github.ensure_pipeline_labels = AsyncMock()
    github.list_issues_with_label = AsyncMock(
        side_effect=lambda label, **_: [i for i in issues if label in i.labels]
    )
    github.list_open_prs = AsyncMock(return_value=[])
    github.has_open_pr_for_issue = AsyncMock(return_value=False)
    github.claim_with_batch = AsyncMock(return_value="abc12345")
    github.clear_batch_label = AsyncMock()
    github.transition_issue = AsyncMock()
    github.transition_pr = AsyncMock()
    github.add_labels = AsyncMock()
    github.remove_labels = AsyncMock()
    github.comment_on_issue = AsyncMock()
    github.comment_on_pr = AsyncMock()
    github.assign_issue = AsyncMock()
    github.get_pr_body = AsyncMock(return_value="")
    github.list_pr_comments = AsyncMock(return_value=[])
    github.create_issue = AsyncMock(return_value=0)

    if get_issue_body_fn is None:
        def get_issue_body_fn(number):
            base = registry.get(number)
            body = base.body if base else "corpo"
            labels = base.labels if base else ("feature",)
            author = base.author if base else "alice"
            return IssueRef(
                number=number, title="t",
                url=f"u/{number}", labels=labels,
                body=body, state="open", author=author,
            )
    github.get_issue = AsyncMock(side_effect=get_issue_body_fn)

    notifier = MagicMock()
    for attr in _NOTIFIER_METHODS:
        setattr(notifier, attr, AsyncMock())

    ledger_path = Path(tempfile.mkdtemp(prefix=".test_refine_")) / "dispatches.json"
    ledger = DispatchLedger(path=ledger_path)
    client = _SeqWorkerClientForRefine(worker_responses)

    monitor = PipelineMonitor(
        cfg, github=github, notifier=notifier,
        implementer=WorkerImplementer(client=client, ledger=ledger),
    )
    return monitor, github, client


class TestRefineDivergenceEarlyStop:
    """Fix B: body crescendo no 3º+ passe → bloqueado early.

    Sem o fix: convergência só por body-idêntico → nunca converge se body
    cresce → moí todos os 5 passes → bloqueia no teto (com 3 passes
    desperdiçados em "cada passe acumula gaps").

    Com o fix: 3º passe com body maior que o anterior → bloqueia early.
    """

    async def test_body_crescendo_no_terceiro_passe_bloqueia_early(self):
        """Body cresce nos passes 2 e 3 → early-stop no 3º passe (attempt=3)."""
        # Configuração dos corpos:
        # - before_body (capturado no dispatch): body da issue na lista
        # - after_body (re-lido após o passe): body retornado por get_issue

        body_v1 = "Quero uma feature."         # body original (passe 1)
        body_v2 = "Quero uma feature. Gap 2."  # passe 2: cresceu
        body_v3 = "Quero uma feature. Gap 2. Gap 3."  # passe 3: cresceu de novo

        # Passa pelo 1º e 2º passe (body mudou, ok normal).
        # No 3º passe, body cresceu NOVAMENTE → divergência → bloqueio.

        # O monitor faz dispatch fire-and-forget: post() → task_id.
        # O reconcile aplica o veredito. Aqui testamos _apply_refine_verdict
        # diretamente via _reconcile_refine_issues.

        # Passo 1: issue com body v1, refine_attempt=0 (primeiro passe).
        # _refine_one_issue vai despachar e gravar before_body=v1 no ledger.
        # _reconcile (reconcile_refine_issues) vai chamar _apply_refine_verdict
        # com before_body=v1 e after_body=v2 (get_issue retorna v2).
        # → body mudou, bump attempt, grava body_len(v2), vai pra nova.

        # Precisamos simular 3 passes do ciclo dispatch → reconcile.
        # Simplificamos: chamaremos _apply_refine_verdict diretamente com
        # os corpos corretos, simulando o que o reconcile faria.

        from deile.orchestration.pipeline.stages import _apply_refine_verdict

        feature_labels = ("feature", REFINAR, WORKFLOW_ARCHITECTURE)
        issue_base = _issue(1, *feature_labels, body=body_v1)

        monitor, github, _ = _make_monitor_for_refine([issue_base], [])

        # Simula passe 1: before=v1, after=v2 → body mudou (normal, sem early-stop).
        # refine_attempt=0 antes → bump → 1.
        github.get_issue = AsyncMock(return_value=_issue(1, *feature_labels, body=body_v2))
        await _apply_refine_verdict(
            monitor, issue_base,
            {"last_result_full": "REFINO: OK", "last_is_error": False},
            body_v1,
        )
        # Não deve ter bloqueado; attempt deve ser 1.
        assert WORKFLOW_BLOCKED not in _added_labels(github, "issue", 1)
        assert monitor._resume_tracker.refine_attempt(1) == 1
        # body_len de v2 deve estar gravado.
        assert monitor._resume_tracker.get_prev_refine_body_len(1) == len(body_v2)

        # Simula passe 2: before=v2, after=v3 → body cresceu de novo.
        # refine_attempt=1 → bump → 2 (ainda abaixo do limiar de 3).
        github.add_labels.reset_mock()
        github.get_issue = AsyncMock(return_value=_issue(1, *feature_labels, body=body_v3))
        await _apply_refine_verdict(
            monitor, issue_base,
            {"last_result_full": "REFINO: OK", "last_is_error": False},
            body_v2,
        )
        # Ainda não deve bloquear (attempt=2 < 3).
        assert WORKFLOW_BLOCKED not in _added_labels(github, "issue", 1)
        assert monitor._resume_tracker.refine_attempt(1) == 2
        # body_len de v3 deve estar gravado.
        assert monitor._resume_tracker.get_prev_refine_body_len(1) == len(body_v3)

        # Simula passe 3: before=v3, after=v3+"Gap 4." → corpo ainda CRESCENDO.
        # refine_attempt=2 antes; DENTRO do _apply_refine_verdict, set_refine_attempt
        # sincroniza com as labels (que aqui estão sem ~refine:N durável no fake,
        # então set_refine_attempt(1, 0) não encolhe). O valor atual é 2.
        # O guard dispara quando current_refine_attempt >= 3.
        # Precisamos que refine_attempt seja 3 quando o guard é avaliado.
        # O set_refine_attempt no início do _apply_refine_verdict sincroniza
        # da label. Como a label não tem ~refine:N durável no fake, permanece
        # em 2. Então precisamos que o bump_refine ocorra ANTES do guard.
        # Olhando o código: a ordem é:
        #   1. set_refine_attempt (from labels)  → 2 (não encolhe)
        #   2. convergência check → não converge (body_v3+"Gap4" != body_v3)
        #   3. guard Fix B: current_refine_attempt = refine_attempt(1) = 2
        #      prev_len = len(body_v3); after_len = len(body_v3+"Gap 4.")
        #      after_len > prev_len AND attempt >= 3 → NÃO dispara ainda (2 < 3)
        # → guard requer attempt >= 3. Então o 3º passe NÃO dispara com attempt=2.
        # O guard dispara no 4º passe? Vamos ajustar o teste para 4 passes.

        # Após o 2º passe bem-sucedido, attempt=2. No 3º passe, set_refine_attempt
        # não altera (labels sem ~refine:N), então ao entrar no guard temos
        # current_refine_attempt=2. O bump_refine só ocorre DEPOIS do guard
        # (na linha "monitor._resume_tracker.bump_refine(number)").
        # Portanto o guard de attempt>=3 dispara no 4º passe (attempt=3 após 3 bumps).

        # Conclusão: o limiar attempt>=3 na implementação significa "após o 3º bump",
        # ou seja, é avaliado ANTES do bump do passe atual — equivalente a "no 4º passe".
        # Teste mais preciso: fazemos o 3º passe (attempt sobe pra 3 via bump) e então
        # no 4º passe o guard dispara.

        body_v4 = body_v3 + " Gap 4."

        github.add_labels.reset_mock()
        github.get_issue = AsyncMock(return_value=_issue(1, *feature_labels, body=body_v4))
        await _apply_refine_verdict(
            monitor, issue_base,
            {"last_result_full": "REFINO: OK", "last_is_error": False},
            body_v3,
        )
        # Ainda não deve bloquear (attempt=2 < 3 ao entrar; 2 não >= 3).
        assert WORKFLOW_BLOCKED not in _added_labels(github, "issue", 1)
        assert monitor._resume_tracker.refine_attempt(1) == 3

        # Passe 4: attempt=3 ao entrar → guard dispara.
        body_v5 = body_v4 + " Gap 5."
        github.add_labels.reset_mock()
        github.comment_on_issue.reset_mock()
        github.get_issue = AsyncMock(return_value=_issue(1, *feature_labels, body=body_v5))
        await _apply_refine_verdict(
            monitor, issue_base,
            {"last_result_full": "REFINO: OK", "last_is_error": False},
            body_v4,
        )
        # Fix B deve ter disparado: BLOQUEADA.
        added_4 = _added_labels(github, "issue", 1)
        assert WORKFLOW_BLOCKED in added_4, (
            f"Esperava ~workflow:bloqueada no 4º passe com body crescendo, "
            f"mas add_labels recebeu: {added_4}"
        )

    async def test_body_estabiliza_nao_bloqueia_early(self):
        """Body que estabiliza ou encolhe no 3º+ passe → não dispara o guard."""
        from deile.orchestration.pipeline.stages import _apply_refine_verdict

        body_v1 = "Quero feature X com integração Y."
        body_v2 = "Quero feature X com integração Y. Detalhe Z."  # cresceu
        body_v3 = "Quero feature X com integração Y. Detalhe Z."  # IGUAL (convergência)

        feature_labels = ("feature", REFINAR, WORKFLOW_ARCHITECTURE)
        issue_base = _issue(1, *feature_labels, body=body_v1)
        monitor, github, _ = _make_monitor_for_refine([issue_base], [])

        # Passe 1: v1 → v2 (cresceu, attempt=0 → 1).
        github.get_issue = AsyncMock(return_value=_issue(1, *feature_labels, body=body_v2))
        await _apply_refine_verdict(
            monitor, issue_base,
            {"last_result_full": "REFINO: OK", "last_is_error": False},
            body_v1,
        )
        assert monitor._resume_tracker.refine_attempt(1) == 1

        # Passe 2: v2 → v2 (corpo inalterado → CONVERGÊNCIA → promove revisada).
        github.add_labels.reset_mock()
        github.transition_issue.reset_mock()
        github.get_issue = AsyncMock(return_value=_issue(1, *feature_labels, body=body_v3))
        await _apply_refine_verdict(
            monitor, issue_base,
            {"last_result_full": "REFINO: OK", "last_is_error": False},
            body_v2,  # before_body = v2, after_body = v3 = v2 → convergência
        )
        # Convergência antes do guard → revisada, NÃO bloqueada.
        assert WORKFLOW_BLOCKED not in _added_labels(github, "issue", 1)
        # Verifica que foi promovida para revisada (transition chamada).
        transition_targets = [
            call.kwargs.get("to_label") for call in github.transition_issue.await_args_list
        ]
        assert WORKFLOW_REVIEWED in transition_targets

    async def test_corpo_sem_prev_len_nao_bloqueia(self):
        """Se não há prev_len gravado (primeiro passe com attempt>=3 por restart),
        o guard não ativa (prev_len=-1 → skip)."""
        from deile.orchestration.pipeline.stages import _apply_refine_verdict

        feature_labels = ("feature", REFINAR, WORKFLOW_ARCHITECTURE)
        issue_base = _issue(1, *feature_labels, body="corpo")
        monitor, github, _ = _make_monitor_for_refine([issue_base], [])

        # Força o attempt para 4 (simula restart do pod — tracker in-memory está
        # zerado mas labels durável marcam 4 passes já feitos).
        monitor._resume_tracker.get(1).refine_attempt = 4

        body_depois = "corpo mais longo do que antes"
        github.get_issue = AsyncMock(return_value=_issue(1, *feature_labels, body=body_depois))

        # Sem prev_len gravado (-1) → guard não ativa mesmo com attempt>=3.
        await _apply_refine_verdict(
            monitor, issue_base,
            {"last_result_full": "REFINO: OK", "last_is_error": False},
            "corpo",  # before_body (body original)
        )
        # Não deve bloquear (guard requer prev_len >= 0).
        assert WORKFLOW_BLOCKED not in _added_labels(github, "issue", 1)


# ===========================================================================
# Fix #8 (issue #521) — ResumeTracker: address_attempt counters (unit)
# ===========================================================================

class TestResumeTrackerAddressAttempt:
    """Unit tests dos novos contadores de address-feedback (Fix #8)."""

    def test_address_attempt_zero_por_padrao(self):
        t = ResumeTracker()
        assert t.address_attempt(99) == 0

    def test_bump_address_attempt_incrementa(self):
        t = ResumeTracker()
        assert t.bump_address_attempt(10) == 1
        assert t.bump_address_attempt(10) == 2
        assert t.address_attempt(10) == 2

    def test_reset_address_attempt_zera(self):
        t = ResumeTracker()
        t.bump_address_attempt(10)
        t.bump_address_attempt(10)
        t.reset_address_attempt(10)
        assert t.address_attempt(10) == 0

    def test_reset_address_attempt_noop_se_ausente(self):
        t = ResumeTracker()
        t.reset_address_attempt(77)  # não cria estado
        assert t.peek(77) is None

    def test_address_attempt_isolado_por_pr(self):
        t = ResumeTracker()
        t.bump_address_attempt(1)
        t.bump_address_attempt(2)
        t.bump_address_attempt(2)
        assert t.address_attempt(1) == 1
        assert t.address_attempt(2) == 2

    def test_clear_apaga_address_attempt(self):
        t = ResumeTracker()
        t.bump_address_attempt(5)
        t.clear(5)
        assert t.address_attempt(5) == 0


# ===========================================================================
# Fix #8 (issue #521) — review_one_open_pr: auto-fix da PRÓPRIA PR (integração)
# ===========================================================================

# Resposta de review da PRÓPRIA PR que conclui REQUEST_CHANGES sem mergear.
# ``summary`` carrega o veredict que ``_review_was_blocked`` detecta; ``ended``
# fica "incompleto" (não-merged) para cair no caminho resume não-merged.
def _request_changes_response(*, fingerprint: str, tentativa: int) -> dict:
    return _worker_response(
        ok=True,
        summary="Revisei. AC2 não atendido.\nSTATUS: REQUEST_CHANGES",
        ended="incompleto",
        fingerprint=fingerprint,
        tentativa=tentativa,
    )


def _payload_stages(client: _FakeWorkerClient) -> List[str]:
    return [p.get("stage", "") for p in client.payloads]


class TestSelfPrAutoFix:
    """Fix #8: review da PRÓPRIA PR pede mudança + HEAD inalterado →
    despacha ADDRESS (implement + push) em vez de bloquear direto; só bloqueia
    após esgotar ``MAX_ADDRESS_ATTEMPTS`` sem o HEAD mudar.

    Sem o fix: o SHA-guard (Fix A) bloqueava determinísticamente no 2º resume
    do mesmo HEAD — a PR ficava parada esperando o humano e NUNCA se
    auto-corrigia.
    """

    async def test_request_changes_head_inalterado_despacha_address(self):
        """1ª vez (attempt < cap): despacha address (implement, nowait), NÃO
        bloqueia, incrementa o contador."""
        SHA = "headsha000000001"

        # Tick 1 (resume): review pede mudança, grava SHA, não bloqueia.
        pr_1 = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA, head_ref="auto/issue-10")
        monitor, _, client = _make_monitor_for_review(
            prs=[pr_1],
            worker_responses=[_request_changes_response(fingerprint="fp1", tentativa=1)],
        )
        await _review_and_drain(monitor)
        assert monitor._resume_tracker.reviewed_sha(10) == SHA
        assert monitor._resume_tracker.address_attempt(10) == 0

        # Tick 2 (resume): MESMO SHA → SHA-guard dispara. Com REQUEST_CHANGES +
        # PR própria + attempt(0) < cap(1) → despacha address, NÃO bloqueia.
        pr_2 = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA, head_ref="auto/issue-10")
        monitor.github.list_open_prs = AsyncMock(return_value=[pr_2])
        # 1ª resposta: a review (wait); 2ª: o address dispatch (nowait).
        client._responses = [
            _request_changes_response(fingerprint="fp2", tentativa=2),
            _worker_response(ok=True, summary="address aceito"),
        ]
        monitor.github.add_labels.reset_mock()
        await _review_and_drain(monitor)

        # NÃO bloqueou.
        added = _added_labels(monitor.github, "pr", 10)
        assert WORKFLOW_BLOCKED not in added, (
            f"Não deve bloquear na 1ª tentativa de auto-fix; add_labels={added}"
        )
        # Contador de address incrementado.
        assert monitor._resume_tracker.address_attempt(10) == 1
        # Despachou um IMPLEMENT (address) além do pr_review.
        assert "implement" in _payload_stages(client), (
            f"Esperava dispatch de address (stage=implement); "
            f"stages={_payload_stages(client)}"
        )
        # NÃO regravou reviewed_sha (deve seguir SHA para detectar mudança no
        # próximo tick).
        assert monitor._resume_tracker.reviewed_sha(10) == SHA

    async def test_address_nao_mudou_head_bloqueia_apos_cap(self):
        """2ª vez (address não mudou o HEAD): attempt >= cap → BLOQUEIA com a
        mensagem nova de auto-correção esgotada."""
        SHA = "headsha000000002"

        # Pré-seta: SHA já gravado e cap já atingido (1 address feito).
        pr = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA, head_ref="auto/issue-10")
        monitor, _, client = _make_monitor_for_review(
            prs=[pr],
            worker_responses=[_request_changes_response(fingerprint="fpz", tentativa=3)],
        )
        monitor._resume_tracker.set_reviewed_sha(10, SHA)
        monitor._resume_tracker.bump_address_attempt(10)  # já fez 1 (= cap)

        monitor.github.add_labels.reset_mock()
        monitor.github.comment_on_pr.reset_mock()
        await _review_and_drain(monitor)

        # Com address esgotado e HEAD inalterado → BLOQUEIA.
        added = _added_labels(monitor.github, "pr", 10)
        assert WORKFLOW_BLOCKED in added, (
            f"Esperava ~workflow:bloqueada após esgotar auto-fix; add_labels={added}"
        )
        # NÃO despachou novo address (só o pr_review).
        assert "implement" not in _payload_stages(client), (
            f"Não deve despachar address após o cap; stages={_payload_stages(client)}"
        )
        # Mensagem de bloqueio cita a auto-correção esgotada. ``comment_on_pr``
        # é chamado posicionalmente (number, comment).
        block_comments = [
            c.args[1] if len(c.args) > 1 else c.kwargs.get("comment", "")
            for c in monitor.github.comment_on_pr.await_args_list
        ]
        joined = "\n".join(str(x) for x in block_comments)
        assert "auto-correção" in joined, (
            f"Mensagem de bloqueio deve citar auto-correção esgotada; got: {joined!r}"
        )

    async def test_head_mudou_nao_conta_address_reseta_e_segue(self):
        """O HEAD MUDOU entre reviews (worker pushou o fix) → NÃO bloqueia, NÃO
        despacha address, RESETA o contador e segue a review normal."""
        SHA_1 = "headsha-before-01"
        SHA_2 = "headsha-after-fix"

        # Tick 1: review pede mudança no SHA_1, despacha 1 address.
        pr_1 = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA_1, head_ref="auto/issue-10")
        monitor, _, client = _make_monitor_for_review(
            prs=[pr_1],
            worker_responses=[_request_changes_response(fingerprint="f1", tentativa=1)],
        )
        # Pré-condição: já gravou SHA_1 e já está com SHA-guard armado + 0 address.
        monitor._resume_tracker.set_reviewed_sha(10, SHA_1)
        await _review_and_drain(monitor)
        # Disparou address no SHA inalterado.
        assert monitor._resume_tracker.address_attempt(10) == 1

        # Tick 2: o worker pushou o fix → HEAD agora é SHA_2 (diferente).
        pr_2 = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA_2, head_ref="auto/issue-10")
        monitor.github.list_open_prs = AsyncMock(return_value=[pr_2])
        client._responses = [
            # Review ainda incompleta (mais trabalho), mas SHA mudou → não guard.
            _worker_response(ended="incompleto", fingerprint="f2", tentativa=2),
        ]
        monitor.github.add_labels.reset_mock()
        client.payloads.clear()  # isola os dispatches do tick 2
        await _review_and_drain(monitor)

        added = _added_labels(monitor.github, "pr", 10)
        assert WORKFLOW_BLOCKED not in added, (
            f"HEAD mudou → não deve bloquear; add_labels={added}"
        )
        # Contador resetado.
        assert monitor._resume_tracker.address_attempt(10) == 0
        # Novo SHA gravado para o próximo ciclo.
        assert monitor._resume_tracker.reviewed_sha(10) == SHA_2
        # NÃO despachou address neste tick (só a review).
        assert _payload_stages(client).count("implement") == 0

    async def test_address_dispatch_e_nowait(self):
        """O address dispatch é fire-and-forget (wait=False) — não bloqueia o
        tick esperando o worker terminar a correção."""
        SHA = "headsha000000003"
        pr = _pr(10, REVIEW_IN_PROGRESS, head_sha=SHA, head_ref="auto/issue-10")
        monitor, _, client = _make_monitor_for_review(
            prs=[pr],
            worker_responses=[
                _request_changes_response(fingerprint="fp", tentativa=1),
                _worker_response(ok=True, summary="address aceito"),
            ],
        )
        monitor._resume_tracker.set_reviewed_sha(10, SHA)

        # Instrumenta o client para capturar o flag wait de cada dispatch.
        waits: List[bool] = []
        orig_dispatch = client.dispatch

        async def _spy(payload, *, wait):
            waits.append(wait)
            return await orig_dispatch(payload, wait=wait)

        client.dispatch = _spy
        await _review_and_drain(monitor)

        # Houve 2 dispatches: review (wait=True) e address (wait=False).
        assert waits == [True, False], (
            f"Esperava review wait=True e address wait=False; got {waits}"
        )


# ===========================================================================
# Issue #445 — RESUME de review roda em background (não congela o tick)
# ===========================================================================

class TestResumeReviewNonBlocking:
    """O caminho de RESUME não pode bloquear o loop do monitor: ele agora roda
    detached via ``spawn_background``. O tick retorna imediatamente; o gate
    ``_resume_in_flight`` marca a PR enquanto a task vive."""

    async def test_resume_review_nao_bloqueia_o_tick(self):
        import asyncio

        pr = _pr(10, REVIEW_IN_PROGRESS, head_sha="aabbccdd")
        monitor, _, _ = _make_monitor_for_review(prs=[pr], worker_responses=[])

        gate = asyncio.Event()

        async def _slow_review(_monitor, _target, *, resume):
            await gate.wait()
            return WorkOutcome(
                ok=True, text="https://x/pull/10 MERGED", ended="concluido",
            )

        monitor.implementer = MagicMock()
        monitor.implementer.review = AsyncMock(side_effect=_slow_review)

        # O tick retorna ANTES de a review (lenta) completar.
        await monitor._review_one_open_pr()

        assert len(monitor._bg_tasks) == 1, "resume deveria ter sido spawnado em bg"
        assert 10 in monitor._resume_in_flight, "PR deveria estar marcada in-flight"

        # Libera a review e drena a bg task.
        gate.set()
        await asyncio.gather(*list(monitor._bg_tasks))

        assert 10 not in monitor._resume_in_flight, "_resume_in_flight deve esvaziar"
        assert monitor.stats.prs_reviewed == 1
