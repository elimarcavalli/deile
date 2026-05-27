"""Pluggable implementation/review strategy for the autonomous pipeline.

The pipeline used to be hardwired to ``claude -p`` (Claude Code one-shot) for
the *implement* and *review* stages. This module introduces a strategy so the
heavy work can instead be dispatched to **another DEILE** — the long-running
``deile-worker`` Pod — over HTTP. Claude becomes one configurable option among
two, not a hard dependency.

Two strategies:

- :class:`ClaudeImplementer` — legacy path. Creates a local git worktree and
  runs ``claude -p`` inside it. Behaviour is preserved verbatim from the
  original inline code in :mod:`stages`.
- :class:`WorkerImplementer` — DEILE-to-DEILE path. POSTs a brief to the
  ``deile-worker`` control plane (:mod:`deile.infrastructure.deile_worker_client`).
  The worker clones the repo, branches, implements/reviews, runs tests and
  opens/merges the PR inside its own isolated workspace — so no local worktree
  is created on the pipeline side.

The monitor holds a single ``implementer`` (selected by
``PipelineConfig.dispatch_mode``); the stage handlers in :mod:`stages` delegate
the "do the work" step to it and keep the GitHub label orchestration to
themselves.
"""

from __future__ import annotations

import inspect
import logging
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from deile.orchestration.pipeline.briefs import (
    _render_claude_mention_prompt, _render_worker_critique_brief,
    _render_worker_decompose_brief, _render_worker_implement_brief,
    _render_worker_implement_resume_brief, _render_worker_mention_brief,
    _render_worker_pr_address_brief, _render_worker_refine_brief,
    _render_worker_review_brief, _render_worker_review_only_brief,
    _render_worker_review_resume_brief)
from deile.orchestration.pipeline.claude_dispatcher import (
    render_implement_prompt, render_review_prompt)
from deile.orchestration.pipeline.dispatch_resolver import (
    get_endpoint_for, resolve_stage_dispatcher)
from deile.orchestration.pipeline.labels import (issue_type_from_labels,
                                                 persona_for_type,
                                                 template_for_type)
from deile.orchestration.pipeline.model_resolver import resolve_stage_model

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
    from deile.orchestration.pipeline.github_client import (IssueRef,
                                                            MentionTrigger,
                                                            PrRef)
    from deile.orchestration.pipeline.monitor import PipelineMonitor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkOutcome:
    """Result of one implement/review/mention unit of work.

    ``text`` is the agent's stdout (Claude) or final summary (worker); the
    stage handler scans it for a PR URL / the word ``merged``. ``error``
    carries a short diagnostic when ``ok`` is False (surfaced to Discord).

    Resume fields (issue #254) are populated only on the deile-worker path
    when a ``resume`` context was sent; they carry the worker's GROUND-TRUTH
    structured result so the stage handler can decide concluido/incompleto/
    bloqueado without trusting the model's output format:

    - ``ended`` — ``"concluido"`` | ``"incompleto"`` | ``"bloqueado"`` | ``""``
      (empty when the worker returned no structured block, e.g. Claude path).
    - ``pr_url`` — confirmed PR URL the worker saw (may be empty).
    - ``motivo_bloqueio`` — agent-declared ``BLOQUEADO:`` reason (when blocked).
    - ``motivo_fim_loop`` — how the tool-loop ended (timeout/cap/natural/erro).
    - ``fingerprint`` — substantive-change hash for the progress guard.
    - ``tentativa`` — 1-based attempt counter persisted in the workspace.
    - ``budget_acumulado_s`` — accumulated wall-clock across attempts (ceiling).
    """

    ok: bool
    text: str
    error: str = ""
    ended: str = ""
    pr_url: str = ""
    motivo_bloqueio: str = ""
    motivo_fim_loop: str = ""
    fingerprint: str = ""
    tentativa: int = 0
    budget_acumulado_s: float = 0.0
    # Issue #309 fase 3.5: identidade do trabalho retornada pelo worker,
    # persistida no DispatchLedger pro próximo dispatch poder retomar com
    # ``--resume``. Vazias quando o worker não retornou (deile-worker antigo
    # ou erro de transporte antes do response).
    task_id: str = ""
    session_id: str = ""


# --- Refinement-gate verdict parsers (issue #257) ----------------------------
# The critique/refine/decompose briefs end with a strict last-line verdict; these
# parse the LAST matching line from the agent's final text (``WorkOutcome.text``).
# Defaults err on the SAFE side: a missing critique verdict reads as POOR (do not
# advance an unjudged issue); a missing refine verdict reads as ``unknown`` (retry).
# Tolerate markdown decoration around the keyword. The brief says "na ÚLTIMA
# LINHA escreva SOMENTE …" but personas habitually wrap the verdict in **bold**,
# headers (`### VEREDITO`), blockquotes (`> VEREDITO`) or list bullets — and the
# old strict `^\s*VEREDITO:` regex defaulted every decorated answer to "POBRE/
# veredito ausente", feeding an infinite refine→re-critique loop on #281/#283.
_MD_PFX = r"[*_#>\s\-]*"  # leading markdown decoration (zero or more)
_CRITIQUE_RE = re.compile(
    rf"{_MD_PFX}VEREDITO[*_:\s]*\**\s*(CLARO|VAGO)\b\s*[:\-]?\s*\**\s*([^\n*_]*)",
    re.IGNORECASE,
)
_REFINE_RE = re.compile(
    rf"{_MD_PFX}REFINO[*_:\s]*\**\s*(OK|AGUARDA_STAKEHOLDER)\b",
    re.IGNORECASE,
)
_DECOMPOSE_RE = re.compile(
    rf"{_MD_PFX}DECOMPOSTO[*_:\s]*\**\s*([^\n]+)",
    re.IGNORECASE,
)


def _tail_text(text: str, n_lines: int = 5) -> str:
    """Return the last *n_lines* non-empty lines joined by newline.

    Used by the verdict-fallback heuristics: the brief says "na ÚLTIMA LINHA
    escreva SOMENTE …", so the verdict word always sits near the end. Looking
    at the tail bounds false-positives from prose elsewhere in the text.
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n_lines:])


def parse_critique_verdict(text: str) -> Tuple[bool, str]:
    """Return ``(is_clear, reason)`` from a critique outcome. Missing → POOR.

    Two-stage matcher:
      1. Strict regex (markdown-tolerant) anywhere in the text.
      2. Fallback: look for a standalone ``CLARO`` or ``VAGO`` token in the
         last 5 non-empty lines — catches even more exotic decorations
         (emoji, numbered list, multi-line bold, etc.). Returns the inferred
         verdict ONLY when exactly one of the two tokens is present in the
         tail (ambiguous tails fall through to POOR, the safe default).

    Personas can decorate the verdict in countless ways; the fallback keeps
    relaxing the input space without ever defaulting to "advance" (POOR is
    the only safe miss).
    """
    matches = list(_CRITIQUE_RE.finditer(text or ""))
    if matches:
        m = matches[-1]
        is_clear = m.group(1).upper() == "CLARO"
        return is_clear, (m.group(2) or "").strip()
    tail = _tail_text(text)
    has_claro = re.search(r"\bCLARO\b", tail, re.IGNORECASE) is not None
    has_vago = re.search(r"\bVAGO\b", tail, re.IGNORECASE) is not None
    if has_vago and not has_claro:
        rm = re.search(r"\bVAGO\b\s*[:\-\.]?\s*([^\n*_]{0,200})", tail, re.IGNORECASE)
        reason = (rm.group(1) if rm else "").strip()
        return False, reason or "VAGO (inferido do fim do texto)"
    if has_claro and not has_vago:
        return True, "CLARO (inferido do fim do texto)"
    return False, "veredito de crítica ausente"


def parse_refine_verdict(text: str) -> str:
    """Return ``"ok"`` | ``"waiting"`` | ``"unknown"`` from a refine outcome.

    Mirrors ``parse_critique_verdict``: strict regex first, then a tail-line
    fallback that looks for ``AGUARDA_STAKEHOLDER`` or ``OK`` as standalone
    tokens. ``unknown`` is the safe default (caller retries).
    """
    matches = list(_REFINE_RE.finditer(text or ""))
    if matches:
        return "waiting" if matches[-1].group(1).upper() == "AGUARDA_STAKEHOLDER" else "ok"
    tail = _tail_text(text)
    has_waiting = re.search(r"\bAGUARDA_STAKEHOLDER\b", tail, re.IGNORECASE) is not None
    has_ok = re.search(r"\bREFINO[:\s*_]+OK\b", tail, re.IGNORECASE) is not None
    if has_waiting and not has_ok:
        return "waiting"
    if has_ok and not has_waiting:
        return "ok"
    return "unknown"


def parse_decompose_result(text: str) -> List[int]:
    """Return the derived issue numbers reported by a decompose outcome.

    Two-stage: strict ``DECOMPOSTO: …`` regex first; fallback collects ``#NN``
    references from the last 8 lines (architect frequently lists the created
    issues right above the verdict). Returns an empty list on ambiguity.
    """
    matches = list(_DECOMPOSE_RE.finditer(text or ""))
    if matches:
        return [int(n) for n in re.findall(r"#(\d+)", matches[-1].group(1))]
    tail = _tail_text(text, n_lines=8)
    found = re.findall(r"#(\d+)", tail)
    return [int(n) for n in found]


class PipelineImplementer(ABC):
    """Strategy that performs the implement / review / mention work."""

    name: str = "base"

    # Refinement-gate steps (issue #257) — default to "not supported" so the
    # legacy Claude path inherits a graceful no-op; the worker path overrides
    # them. They are NOT abstract on purpose (only the worker implements them).
    async def critique(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        return WorkOutcome(ok=False, text="", error="critique não suportado nesta estratégia")

    async def refine(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        return WorkOutcome(ok=False, text="", error="refine não suportado nesta estratégia")

    async def decompose(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        return WorkOutcome(ok=False, text="", error="decompose não suportado nesta estratégia")

    @abstractmethod
    async def implement(
        self, monitor: "PipelineMonitor", issue: "IssueRef", *, resume: bool = False
    ) -> WorkOutcome:
        ...

    @abstractmethod
    async def review(
        self, monitor: "PipelineMonitor", pr: "PrRef", *, resume: bool = False
    ) -> WorkOutcome:
        ...

    @abstractmethod
    async def mention(
        self,
        monitor: "PipelineMonitor",
        ref: "MentionTrigger",
        *,
        trigger_types: list[str] | None = None,
        all_triggers: list["MentionTrigger"] | None = None,
        mode: str = "comment",
        resume: bool = False,
    ) -> WorkOutcome:
        ...


# ---------------------------------------------------------------------------
# Claude Code one-shot (legacy strategy)
# ---------------------------------------------------------------------------


class ClaudeImplementer(PipelineImplementer):
    """Run ``claude -p`` inside a local git worktree (legacy default).

    Uses ``monitor.worktrees`` + ``monitor.claude`` exactly as the original
    inline stage code did, so injecting a mocked ``claude``/``worktrees`` keeps
    behaving identically.
    """

    name = "claude"

    async def _run_in_worktree(
        self,
        monitor: "PipelineMonitor",
        branch: str | None,
        prompt: str,
        *,
        label: str,
        force_recreate: bool = False,
    ) -> WorkOutcome:
        """Setup a worktree (if ``branch`` is given), then run ``claude``.

        ``branch=None`` skips worktree setup and runs ``claude`` at
        ``monitor.config.base_repo_path`` — used by the mention path which
        has no per-issue branch.

        Worktree creation failures become a failed ``WorkOutcome`` with the
        ``worktree:`` prefix; ``label`` is used in the exception log.
        """
        if branch is None:
            cwd = monitor.config.base_repo_path
        else:
            try:
                worktree = await monitor.worktrees.create_branch_worktree(
                    branch, force_recreate=force_recreate
                )
            except Exception as exc:  # noqa: BLE001 — surface as failed outcome
                logger.exception("worktree setup for %s failed", label)
                return WorkOutcome(
                    ok=False, text="", error=f"worktree: {type(exc).__name__}: {exc}"
                )
            cwd = worktree.path
        result = await monitor.claude.run(prompt, cwd=cwd)
        return WorkOutcome(ok=result.ok, text=result.stdout, error=result.stderr.strip())

    async def implement(
        self, monitor: "PipelineMonitor", issue: "IssueRef", *, resume: bool = False
    ) -> WorkOutcome:
        # ``resume`` is accepted for interface parity. The Claude path already
        # reuses an existing worktree (``force_recreate=False``) so partial work
        # in the worktree survives between attempts; it has no structured
        # ground-truth contract (that lives in the deile-worker path), so the
        # flag does not change behaviour here beyond the existing reuse.
        branch = monitor.branch_for_issue(issue.number)
        prompt = render_implement_prompt(
            monitor.config.repo, issue.number, issue.title, issue.body,
            forge=monitor.forge.config,
        )
        return await self._run_in_worktree(
            monitor, branch, prompt, label=f"#{issue.number}"
        )

    async def review(
        self, monitor: "PipelineMonitor", pr: "PrRef", *, resume: bool = False
    ) -> WorkOutcome:
        worktree_branch = pr.head_ref or f"pr/{pr.number}"
        prompt = render_review_prompt(
            monitor.config.repo, pr.number, pr.title, forge=monitor.forge.config,
        )
        return await self._run_in_worktree(
            monitor, worktree_branch, prompt, label=f"PR #{pr.number}"
        )

    async def mention(
        self,
        monitor: "PipelineMonitor",
        ref: "MentionTrigger",
        *,
        trigger_types: list[str] | None = None,
        all_triggers: list["MentionTrigger"] | None = None,
        mode: str = "comment",
        resume: bool = False,
    ) -> WorkOutcome:
        # ``mode``/``resume`` are accepted for interface parity with the worker
        # path; the legacy Claude path keeps its single context-aware prompt.
        prompt = _render_claude_mention_prompt(
            monitor.config.repo, ref, trigger_types or [], all_triggers or [],
            forge=monitor.forge.config,
        )
        # mention runs at base_repo_path; no per-issue branch worktree.
        return await self._run_in_worktree(monitor, None, prompt, label="mention")


# ---------------------------------------------------------------------------
# DEILE-to-DEILE via the deile-worker (HTTP)
# ---------------------------------------------------------------------------


def _build_resume_block(
    repo: str,
    main: str,
    branch: str,
    *,
    resume: bool,
    expect_merge: bool,
    pr_url_hint: str = "",
) -> dict:
    """Assemble the ``resume`` wire block sent to the worker (issue #254).

    Sent on EVERY pipeline dispatch (fresh and resume) so the worker always
    returns a structured ground-truth result and seeds ``.deile-progress.json``
    — ``mode`` tells the worker whether this was a fresh start or a resume, but
    the brief (not this block) decides reset-vs-keep. ``expect_merge`` is True
    for the review/merge stage so "done" requires a confirmed merge, not just a
    PR URL.
    """
    return {
        "mode": "resume" if resume else "fresh",
        "repo": repo,
        "branch": branch,
        "main_branch": main,
        "expect_merge": expect_merge,
        "pr_url_hint": pr_url_hint,
    }


def _outcome_from_worker_response(data: object) -> WorkOutcome:
    """Map a worker dispatch response dict to a :class:`WorkOutcome`.

    Reads the legacy ``ok``/``summary``/``error`` fields AND the structured
    ``resume`` block (issue #254) when present, so the stage handler gets the
    ground-truth ``ended``/``pr_url``/``motivo_bloqueio``/``fingerprint``/
    ``tentativa`` without re-parsing the worker's free-text summary.
    """
    if not isinstance(data, dict):
        return WorkOutcome(ok=False, text="", error="worker returned non-dict response")
    ok = bool(data.get("ok"))
    text = str(data.get("summary") or data.get("stdout") or "")
    resume_block = data.get("resume")
    fields: dict = {}
    if isinstance(resume_block, dict):
        fields = {
            "ended": str(resume_block.get("ended") or ""),
            "pr_url": str(resume_block.get("pr_url") or ""),
            "motivo_bloqueio": str(resume_block.get("motivo_bloqueio") or ""),
            "motivo_fim_loop": str(resume_block.get("motivo_fim_loop") or ""),
            "fingerprint": str(resume_block.get("fingerprint") or ""),
            "tentativa": int(resume_block.get("tentativa") or 0),
            "budget_acumulado_s": float(resume_block.get("budget_acumulado_s") or 0.0),
        }
    # Issue #309 fase 3.5: extrai task_id/session_id pra persistir no
    # DispatchLedger. claude-worker SEMPRE retorna ambos; deile-worker
    # retorna task_id; campo session_id ausente vira string vazia.
    task_id = str(data.get("task_id") or "")
    session_id = str(data.get("session_id") or "")
    if ok:
        return WorkOutcome(
            ok=True, text=text, error="",
            task_id=task_id, session_id=session_id, **fields,
        )
    err = str(data.get("error") or data.get("summary") or "worker reported failure")
    # Issue #309 fase 3 — resiliência auth: o claude-worker server detecta
    # OAuth expirado/inválido no output do ``claude -p`` e devolve
    # ``error_code=WORKER_AUTH_EXPIRED`` no body. Prefixar o ``error`` com
    # o code permite o monitor distinguir essa falha das genéricas e
    # marcar a PR/issue como ``~workflow:bloqueada`` com comment claro
    # apontando o operador pra ``deploy.py k8s claude-renew``.
    error_code = data.get("error_code")
    if error_code:
        err = f"[{error_code}] {err}"
    return WorkOutcome(
        ok=False, text=text, error=err[:500],
        task_id=task_id, session_id=session_id, **fields,
    )


class WorkerImplementer(PipelineImplementer):
    """Dispatch implement/review/mention work to the ``deile-worker`` Pod.

    The worker is another DEILE running the full toolset behind an HTTP
    control plane. It clones the repo, branches, implements/reviews, runs
    tests and opens/merges the PR in its own isolated, per-channel workspace.
    The pipeline-side ``channel_id`` is synthetic (``pipeline-issue-<N>`` /
    ``pipeline-pr-<N>``) so each work item gets a stable, reusable sandbox.
    """

    name = "deile_worker"

    def __init__(
        self,
        client: Optional[object] = None,
        *,
        endpoint_override: Optional[str] = None,
        ledger: Optional["DispatchLedger"] = None,
    ) -> None:
        """Constrói o implementer.

        Args:
            client: Cliente HTTP do worker (default: :class:`DeileWorkerClient`).
                Em testes, injetado como fake/mock.
            endpoint_override: URL HTTP absoluta que sobrescreve a resolução
                per-stage (issue #309 fase 2). Útil em testes ad-hoc e dev
                local apontando para localhost. Quando ``None`` (default), o
                endpoint é resolvido via :func:`resolve_stage_dispatcher` +
                :func:`get_endpoint_for` a cada chamada.
            ledger: :class:`DispatchLedger` pra rastrear task_id/session_id
                entre dispatches (issue #309 fase 3.5 — resume mecânica).
                Default = singleton em ``~/.deile/pipeline/dispatches.json``.
                Em testes, injetado com path em tmp_path.
        """
        if client is None:
            from deile.infrastructure.deile_worker_client import \
                DeileWorkerClient
            client = DeileWorkerClient()
        self._client = client
        self._endpoint_override = endpoint_override
        if ledger is None:
            from deile.orchestration.pipeline.dispatch_ledger import \
                DispatchLedger
            ledger = DispatchLedger()
        self._ledger = ledger

    def _resolve_endpoint(self, stage: str) -> str:
        """Resolve a URL HTTP do worker pod que recebe o dispatch de *stage*.

        Precedência:
          1. ``endpoint_override`` passado no ``__init__`` (absoluto).
          2. ``resolve_stage_dispatcher(stage)`` → :func:`get_endpoint_for`
             — chain de env vars + default ``deile-worker``.

        ``stage`` precisa estar em :data:`PIPELINE_STAGES`; o ValueError
        levantado pelo resolver propaga (programming bug, não user input).
        """
        if self._endpoint_override:
            return self._endpoint_override
        return get_endpoint_for(resolve_stage_dispatcher(stage))

    async def _post_dispatch(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        wait: bool,
    ) -> Dict[str, Any]:
        """Costura HTTP — recebe URL explícita resolvida pelo caller.

        Único ponto que toca o client; em testes, é patchado para isolar
        o roteamento per-stage do I/O real. ``url`` é a URL completa do
        worker pod (sem ``/v1/dispatch``); o client adiciona o path.

        Por compat com clients fakes existentes (``test_implementer.py`` e
        ``test_implementer_per_stage_model.py``) que NÃO aceitam
        ``endpoint_url``, usa-se introspecção para só passar o kwarg
        quando o client real o suporta — assim a refatoração não
        quebra fakes pré-existentes.
        """
        sig = inspect.signature(self._client.dispatch)
        if "endpoint_url" in sig.parameters:
            return await self._client.dispatch(
                payload, wait=wait, endpoint_url=url,
            )
        return await self._client.dispatch(payload, wait=wait)

    async def _resolve_resume_meta(
        self,
        ledger_key: Optional[str],
        url: str,
    ) -> Optional[Dict[str, str]]:
        """Pra dispatches em resume mode: consulta o DispatchLedger pelo
        ``ledger_key`` (ex.: ``pr:344``), valida o estado via resume-info do
        worker, e retorna ``{prev_task_id, resume_session_id}`` quando o
        resume é viável. None significa "fallback pra fresh dispatch".

        Cenários:
          - Sem ledger entry → None (primeiro dispatch desse PR).
          - Worker 404/410 (workdir lost, meta missing) → None + limpa
            entrada stale.
          - Worker diz claude_alive=True → None (não disturbar in-flight).
            Pipeline reaper espera o claude terminar ou matar.
          - Tudo OK → retorna meta pra resume.
        """
        if ledger_key is None:
            return None
        record = self._ledger.get(ledger_key)
        if record is None:
            return None
        prev_task_id = record.get("task_id")
        if not prev_task_id:
            return None
        # Consulta o worker pelo estado da sessão.
        try:
            info = await self._client.get_resume_info(
                prev_task_id, endpoint_url=url,
            )
        except Exception as exc:  # noqa: BLE001
            # Erro de transporte ou worker — log e fallback pra fresh.
            # Limpa ledger entry pra não consultar resume-info repetidamente.
            from deile.infrastructure.deile_worker_client import \
                WorkerDispatchError
            if isinstance(exc, WorkerDispatchError) and exc.error_code == "NOT_FOUND":
                logger.info(
                    "ledger entry %s aponta task_id=%s sem metadata no worker "
                    "(404) — limpando e fallback fresh",
                    ledger_key, prev_task_id,
                )
                self._ledger.clear(ledger_key)
                return None
            logger.warning(
                "resume-info lookup falhou pra %s task_id=%s: %s — fallback fresh",
                ledger_key, prev_task_id, exc,
            )
            return None
        if not isinstance(info, dict):
            self._ledger.clear(ledger_key)
            return None
        if not info.get("workdir_exists", False):
            logger.info(
                "ledger entry %s task_id=%s tem workdir perdido — fallback fresh",
                ledger_key, prev_task_id,
            )
            self._ledger.clear(ledger_key)
            return None
        if info.get("claude_alive", False):
            # Claude ainda rodando — não despachar de novo pra não matar a
            # sessão em curso. O caller (stage handler) detecta None e
            # mantém em_andamento; próximo tick tenta de novo.
            logger.info(
                "ledger entry %s task_id=%s session=%s ainda alive — "
                "skip dispatch nesse tick",
                ledger_key, prev_task_id, info.get("session_id"),
            )
            return {"_still_alive": True}
        session_id = info.get("session_id") or record.get("session_id")
        if not session_id:
            self._ledger.clear(ledger_key)
            return None
        return {
            "prev_task_id": str(prev_task_id),
            "resume_session_id": str(session_id),
        }

    async def _dispatch(
        self,
        brief: str,
        *,
        channel_id: str,
        persona: str = "developer",
        resume_block: Optional[dict] = None,
        stage: Optional[str] = None,
        branch: Optional[str] = None,
        ledger_key: Optional[str] = None,
        resume: bool = False,
    ) -> WorkOutcome:
        from deile.infrastructure.deile_worker_client import (
            WorkerDispatchError, build_dispatch_payload)

        # Defensive clamp under the 8000-char dispatch cap (issue #257): every
        # body-embedding brief puts the issue/PR body LAST (after the VEREDITO
        # rules), so truncating the tail only trims body context — never the
        # instructions. Guarantees the payload never hard-fails on size.
        if len(brief) > 7950:
            brief = brief[:7950] + "\n…(brief truncado por tamanho)"
        # Per-stage model override (issue #305). ``stage`` is the canonical
        # pipeline-stage name (see :data:`PIPELINE_STAGES`); when set, the
        # resolver returns ``None`` if no override is configured (the worker
        # then falls back to its own ``DEILE_PREFERRED_MODEL``), or a
        # ``provider:model`` slug to pin THIS turn only.
        preferred_model = resolve_stage_model(stage) if stage else None
        # Per-stage endpoint routing (issue #309 fase 2). ``stage`` opcional
        # mantém compat com callers (testes) que ainda não declaram stage.
        url = self._resolve_endpoint(stage or "implement")

        # Issue #309 fase 3.5 — consulta o ledger pra ver se há dispatch
        # anterior retomável; passa ``resume_session_id + prev_task_id``
        # no payload quando o worker confirma viabilidade. Quando o
        # caller passa ``resume=True`` mas ledger não tem nada (ex.:
        # primeira chance de resume após restart do pipeline), fallback
        # automático pra fresh dispatch — sem perder o ciclo.
        resume_meta: Optional[Dict[str, str]] = None
        if resume and ledger_key:
            resume_meta = await self._resolve_resume_meta(ledger_key, url)
            if resume_meta and resume_meta.get("_still_alive"):
                # Worker confirmou claude ainda alive — não dispatch.
                # Devolve outcome "em curso" pra stage handler decidir
                # (mantém em_andamento, próximo tick re-checa).
                return WorkOutcome(
                    ok=False, text="",
                    error="DISPATCH_SKIPPED_STILL_RUNNING: claude-worker "
                          "ainda rodando o task anterior; skip nesse tick",
                )

        # ``stage=stage`` propaga o stage canônico pro worker (issue #309
        # fase 2 hotfix): SEM isso o claude_worker_server caia no default
        # ``implement`` para TODOS os dispatches (review/refine/follow_ups
        # eram todos registrados como implement, quebrando o preamble do
        # pr_review e enganando telemetry).
        payload_kwargs: Dict[str, Any] = dict(
            brief=brief, channel_id=channel_id, persona=persona, wait=True,
            preferred_model=preferred_model, stage=stage, branch=branch,
        )
        if resume_meta:
            payload_kwargs["resume_session_id"] = resume_meta["resume_session_id"]
            payload_kwargs["prev_task_id"] = resume_meta["prev_task_id"]
        payload = build_dispatch_payload(**payload_kwargs)
        # The resume context (issue #254) is an additive wire field consumed by
        # the worker; ``build_dispatch_payload`` validates the core fields, so
        # we attach ``resume`` after building to keep that contract untouched.
        if resume_block:
            payload["resume"] = resume_block
        try:
            data = await self._post_dispatch(url, payload, wait=True)
        except WorkerDispatchError as exc:
            return WorkOutcome(ok=False, text="", error=f"{exc.error_code}: {exc}"[:500])
        except Exception as exc:  # noqa: BLE001 — never crash the tick
            logger.exception("worker dispatch raised")
            return WorkOutcome(ok=False, text="", error=f"{type(exc).__name__}: {exc}"[:500])
        outcome = _outcome_from_worker_response(data)
        # Issue #309 fase 3.5 — persistência no DispatchLedger.
        if ledger_key and outcome.task_id:
            if outcome.ok:
                # Trabalho completado com sucesso: limpa entrada (próximo
                # dispatch desse PR/issue será fresh).
                self._ledger.clear(ledger_key)
            else:
                # Trabalho incompleto (erro, timeout, blocked): grava ou
                # atualiza pra resume no próximo tick.
                worker_kind = "claude" if "claude-worker" in url else "deile"
                self._ledger.record(
                    ledger_key,
                    task_id=outcome.task_id,
                    session_id=outcome.session_id,
                    stage=stage, branch=branch,
                    worker_kind=worker_kind,
                )
        return outcome

    async def implement(
        self, monitor: "PipelineMonitor", issue: "IssueRef", *, resume: bool = False
    ) -> WorkOutcome:
        branch = monitor.branch_for_issue(issue.number)
        forge_cfg = monitor.forge.config
        render = (
            _render_worker_implement_resume_brief if resume
            else _render_worker_implement_brief
        )
        brief = render(
            monitor.config.repo, monitor.config.main_branch, branch,
            issue.number, issue.title, issue.body, forge=forge_cfg,
        )
        resume_block = _build_resume_block(
            monitor.config.repo, monitor.config.main_branch, branch,
            resume=resume, expect_merge=False,
        )
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
        return await self._dispatch(
            brief, channel_id=f"pipeline-issue-{issue.number}",
            resume_block=resume_block, stage="implement", branch=branch,
            ledger_key=DispatchLedger.key_for_issue(issue.number), resume=resume,
        )

    # --- Refinement gate (issue #257) -------------------------------------
    # critique/refine route to the persona that owns the issue type (analyst for
    # intent, architect for feature/refactor, debugger for bug); decompose is
    # always the architect. No resume_block: these steps open no PR, so the
    # worker returns a plain ok+summary and the verdict lives in its last line.

    async def critique(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        issue_type = issue_type_from_labels(issue.labels)
        brief = _render_worker_critique_brief(
            monitor.config.repo, issue.number, issue.title, issue.body,
            issue_type=issue_type or "", template=template_for_type(issue_type) or "intent.md",
            forge=monitor.forge.config,
        )
        return await self._dispatch(
            brief, channel_id=f"pipeline-issue-{issue.number}",
            persona=persona_for_type(issue_type), stage="refine",
        )

    async def refine(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        issue_type = issue_type_from_labels(issue.labels)
        brief = _render_worker_refine_brief(
            monitor.config.repo, issue.number, issue.title, issue.body,
            issue_type=issue_type or "", template=template_for_type(issue_type) or "intent.md",
            forge=monitor.forge.config,
        )
        return await self._dispatch(
            brief, channel_id=f"pipeline-issue-{issue.number}",
            persona=persona_for_type(issue_type), stage="refine",
        )

    async def decompose(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        brief = _render_worker_decompose_brief(
            monitor.config.repo, issue.number, issue.title, issue.body,
            forge=monitor.forge.config,
        )
        return await self._dispatch(
            brief, channel_id=f"pipeline-issue-{issue.number}",
            persona="architect", stage="refine",
        )

    async def review(
        self, monitor: "PipelineMonitor", pr: "PrRef", *, resume: bool = False
    ) -> WorkOutcome:
        forge_cfg = monitor.forge.config
        render = (
            _render_worker_review_resume_brief if resume
            else _render_worker_review_brief
        )
        brief = render(
            monitor.config.repo, monitor.config.main_branch, pr.number,
            forge=forge_cfg,
        )
        resume_block = _build_resume_block(
            monitor.config.repo, monitor.config.main_branch,
            pr.head_ref or f"pr/{pr.number}", resume=resume, expect_merge=True,
            pr_url_hint=pr.url,
        )
        # The review/merge stage is the final quality gate: dispatch under the
        # ``reviewer`` persona (instructions in personas/instructions/reviewer.md)
        # so the worker evaluates SOLID/SRP/DRY/KISS/security/idempotency, not
        # just whether the suite is green. implement/mention keep ``developer``.
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
        return await self._dispatch(
            brief, channel_id=f"pipeline-pr-{pr.number}",
            persona="reviewer", resume_block=resume_block, stage="pr_review",
            branch=pr.head_ref or f"pr/{pr.number}",
            ledger_key=DispatchLedger.key_for_pr(pr.number), resume=resume,
        )

    async def mention(
        self,
        monitor: "PipelineMonitor",
        ref: "MentionTrigger",
        *,
        trigger_types: list[str] | None = None,
        all_triggers: list["MentionTrigger"] | None = None,
        mode: str = "comment",
        resume: bool = False,
    ) -> WorkOutcome:
        """Dispatch a mention/assignment by ROLE (issue #253 follow-up).

        ``mode`` (decided by the stage router) selects the brief + persona:

        - ``review_only`` — requested reviewer: review + assign author back, NO
          fix/merge (reviewer persona).
        - ``work_merge`` — assignee on a PR: quality-gate review + resolve
          threads + fix + MERGE (reviewer persona, resume-aware).
        - ``address`` — comment/body mention on a PR: do what was asked +
          resolve threads + push, NO merge (reviewer persona).
        - ``comment`` — comment mention on an issue: do what the comment says
          (developer persona, context-rich brief). Default.
        """
        repo = monitor.config.repo
        main = monitor.config.main_branch
        number = ref.target_number
        channel_id = f"pipeline-mention-{ref.target_kind}-{number}"
        pr_ref = next(
            (t.pr for t in (all_triggers or [ref]) if t.pr is not None), None
        )
        head = (pr_ref.head_ref if pr_ref else "") or f"pr/{number}"
        pr_url_hint = pr_ref.url if pr_ref else ""

        # PR-scoped reviewer modes dispatched under the ``reviewer`` persona
        # with a resume block. ``work_merge`` is the only mode that merges and
        # the only one resume-aware (uses the review-resume brief on retry).
        # ForgeConfig do monitor — usado nos briefs forge-aware (issue #297).
        forge_cfg = monitor.forge.config
        # Stage for per-stage model override (issue #305): PR-scoped modes are
        # review work (``pr_review`` stage); issue-scoped comments are
        # follow-up work (``follow_ups`` stage). See PIPELINE_STAGES.
        if mode in _MENTION_REVIEWER_MODES:
            brief_fn, expect_merge = _MENTION_REVIEWER_MODES[mode]
            if mode == "work_merge" and resume:
                reviewer_brief = _render_worker_review_resume_brief(
                    repo, main, number, forge=forge_cfg,
                )
            else:
                reviewer_brief = brief_fn(repo, main, number, forge=forge_cfg)
            from deile.orchestration.pipeline.dispatch_ledger import \
                DispatchLedger
            return await self._dispatch(
                reviewer_brief, channel_id=channel_id, persona="reviewer",
                resume_block=_build_resume_block(
                    repo, main, head, resume=resume, expect_merge=expect_merge,
                    pr_url_hint=pr_url_hint,
                ),
                stage="pr_review", branch=head,
                # mentions PR-scoped usam mesma chave que pr_review pra que o
                # pipeline reaproveite session se houver resume.
                ledger_key=DispatchLedger.key_for_pr(number)
                          if ref.target_kind == "pr" else None,
                resume=resume,
            )
        # Default: comment mention on an issue → do what the comment says.
        brief = _render_worker_mention_brief(
            repo, ref, trigger_types or [], all_triggers or [], forge=forge_cfg,
        )
        return await self._dispatch(
            brief, channel_id=channel_id, persona="developer",
            stage="follow_ups",
        )


# ---------------------------------------------------------------------------
# Mention mode dispatch table
# ---------------------------------------------------------------------------
# (brief_renderer, expect_merge) for each PR-scoped reviewer mode used in
# ``WorkerImplementer.mention``. ``work_merge`` is the only one that merges
# *and* the only resume-aware mode (the resume brief is selected inline since
# only that mode has a resume variant).
_MENTION_REVIEWER_MODES = {
    "review_only": (_render_worker_review_only_brief, False),
    "work_merge":  (_render_worker_review_brief,      True),
    "address":     (_render_worker_pr_address_brief,  False),
}


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

WORKER_ALIASES = frozenset({"deile_worker", "worker", "deile", "deile-worker"})
CLAUDE_ALIASES = frozenset({"claude", "claude_code", "claude-code"})

# Backwards-compatible aliases for internal callers that used the underscored names.
_WORKER_ALIASES = WORKER_ALIASES
_CLAUDE_ALIASES = CLAUDE_ALIASES


def is_claude_mode(dispatch_mode: Optional[str]) -> bool:
    """Return True if ``dispatch_mode`` selects the Claude strategy.

    Handles ``None``, empty, whitespace, and case variations uniformly so callers
    don't reproduce the ``(mode or "claude").strip().lower() in (...)`` idiom.

    **Validação consistente com :func:`build_implementer`** (PR review iter 2):
    um valor não-vazio fora dos aliases conhecidos dispara :class:`ValueError`,
    em vez de retornar ``False`` silenciosamente (que faria a montagem da
    :class:`PipelineConfig` aplicar worker-semantics ANTES do erro real,
    expondo flags de configuração para um modo inexistente).
    """
    if not dispatch_mode or not dispatch_mode.strip():
        return True
    mode = dispatch_mode.strip().lower()
    if mode in CLAUDE_ALIASES:
        return True
    if mode in WORKER_ALIASES:
        return False
    raise ValueError(
        f"unknown pipeline dispatch_mode {dispatch_mode!r}; "
        f"expected one of {sorted(WORKER_ALIASES | CLAUDE_ALIASES)} "
        "(set DEILE_PIPELINE_DISPATCH_MODE explicitly)"
    )


def _warn_if_claude_unavailable() -> None:
    """Aviso de boot quando dispatch_mode=claude mas ``claude`` não tá no PATH.

    Sem fail-fast: o operador pode estar montando o binary via volume custom
    que ``shutil.which`` ainda não vê (ex.: deploy K8s com initContainer). A
    falha real surge no primeiro dispatch quando o subprocess do ``claude -p``
    estourar ENOENT — mas com este warning na boot o operador já sabe que
    precisa instalar o CLI antes de ver red no painel.

    Issue #309: o image ``deile-stack:local`` hoje NÃO instala o ``claude``
    CLI nem monta credentials em ``~/.claude/``. Painel TUI permite flipar
    para ``claude``, mas a dispatch só funcionará quando o image trouxer o
    binary e o pod tiver credentials montadas (follow-up infra).
    """
    # shutil é import top-level no módulo — patch como
    # ``deile.orchestration.pipeline.implementer.shutil.which`` para isolamento
    # robusto sob test pollution (algum suite gigante reordena imports e
    # patchar ``shutil.which`` global passa a falhar; patchar onde a função
    # de fato lê resolve isso).
    if shutil.which("claude") is None:
        logger.warning(
            "pipeline dispatch_mode=claude mas binary 'claude' não encontrado "
            "no PATH. A próxima dispatch PODE falhar com ENOENT — a menos que "
            "o binary esteja sendo montado por volume/initContainer que "
            "`shutil.which` não enxerga ainda. Para resolver no caminho "
            "comum, instale o claude CLI (ou rode `claude login`) antes de "
            "usar este modo. Detalhe: "
            "deile/orchestration/pipeline/claude_dispatcher.py "
            "usa shutil.which('claude') ou cai em literal 'claude'."
        )


def build_implementer(
    dispatch_mode: Optional[str] = None,
    *,
    worker_client: Optional[object] = None,
) -> PipelineImplementer:
    """Return the pipeline implementer.

    A partir da fase 2 da issue #309, a factory **sempre retorna
    :class:`WorkerImplementer`** — a decisão de qual worker (``deile-worker``
    vs ``claude-worker``) recebe o POST ``/v1/dispatch`` é feita
    per-stage em runtime via :mod:`deile.orchestration.pipeline.dispatch_resolver`,
    NÃO mais na construção do implementer.

    O parâmetro ``dispatch_mode`` é mantido apenas para compat com chamadas
    antigas (``monitor.PipelineMonitor`` ainda passa ``config.dispatch_mode``):
    valor não-vazio fora dos aliases reconhecidos dispara :class:`ValueError`
    (fail-fast em typo, ex.: ``deile_woker``). Valor vazio/``None`` é o
    default normal — nada é validado.

    A classe :class:`ClaudeImplementer` (subprocess ``claude -p`` local)
    continua existindo, mas só para callers locais fora do cluster — use
    :func:`get_local_claude_implementer` em vez de ``build_implementer``
    para esse caso.

    Raises:
        ValueError: ``dispatch_mode`` não-vazio que não case com nenhum
            alias canônico nem legacy (validação via
            :func:`dispatch_resolver.is_valid_dispatcher`).
    """
    if dispatch_mode and dispatch_mode.strip():
        from deile.orchestration.pipeline.dispatch_resolver import \
            is_valid_dispatcher  # noqa: PLC0415
        if not is_valid_dispatcher(dispatch_mode):
            raise ValueError(
                f"unknown pipeline dispatch_mode {dispatch_mode!r}; "
                f"expected one of ('deile-worker', 'claude-worker') "
                f"or legacy aliases (set DEILE_PIPELINE_DISPATCH_MODE explicitly)"
            )
    return WorkerImplementer(client=worker_client)


def get_local_claude_implementer() -> "ClaudeImplementer":
    """Factory exclusiva para construir :class:`ClaudeImplementer` em uso
    local (deile CLI fora do cluster — Humano rodando ``python3 deile.py``
    com ``claude -p`` no PATH).

    Emite o aviso de boot quando o binary ``claude`` não está disponível
    (via :func:`_warn_if_claude_unavailable`) — sem fail-fast, porque o
    operador pode montar o binary via volume que ``shutil.which`` ainda
    não enxerga.

    **NÃO usada pelo pipeline em cluster** — esse caminho passa por
    :func:`build_implementer`, que sempre devolve :class:`WorkerImplementer`
    (decisão per-stage de endpoint via :mod:`dispatch_resolver`).
    """
    _warn_if_claude_unavailable()
    return ClaudeImplementer()
