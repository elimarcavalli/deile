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

import asyncio
import inspect
import json
import logging
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from deile.orchestration.pipeline.briefs import (
    _render_claude_mention_prompt,
    _render_worker_critique_brief,
    _render_worker_decompose_brief,
    _render_worker_implement_brief,
    _render_worker_implement_resume_brief,
    _render_worker_mention_brief,
    _render_worker_pr_address_brief,
    _render_worker_pr_unified_brief,
    _render_worker_refine_brief,
)
from deile.orchestration.pipeline.claude_dispatcher import (
    render_implement_prompt,
    render_review_prompt,
)
from deile.orchestration.pipeline.constants import resolve_forge_repo
from deile.orchestration.pipeline.dispatch_resolver import (
    BUILTIN_DISPATCHERS,
    CLAUDE_ALIASES,
    WORKER_ALIASES,
    get_endpoint_for,
    resolve_stage_dispatcher,
    resolve_stage_max_retries,
    resolve_stage_timeout_s,
)
from deile.orchestration.pipeline.labels import (
    issue_type_from_labels,
    persona_for_type,
    template_for_type,
)
from deile.orchestration.pipeline.model_resolver import (
    resolve_stage_cli_model,
    resolve_stage_model,
)
from deile.orchestration.pipeline.reasoning_resolver import resolve_stage_reasoning

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger
    from deile.orchestration.pipeline.github_client import (
        IssueRef,
        MentionTrigger,
        PrRef,
    )
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
        return (
            "waiting" if matches[-1].group(1).upper() == "AGUARDA_STAKEHOLDER" else "ok"
        )
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
        return WorkOutcome(
            ok=False, text="", error="critique não suportado nesta estratégia"
        )

    async def refine(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        return WorkOutcome(
            ok=False, text="", error="refine não suportado nesta estratégia"
        )

    async def decompose(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        return WorkOutcome(
            ok=False, text="", error="decompose não suportado nesta estratégia"
        )

    async def address_review(
        self, monitor: "PipelineMonitor", pr: "PrRef"
    ) -> WorkOutcome:
        """Apply the reviewer's REQUEST_CHANGES feedback on our OWN PR (Fix #8).

        Default falls back to a fresh ``review`` dispatch so the legacy Claude
        path stays functional; the worker overrides this with a dedicated
        implement-style dispatch that writes code + pushes (never reviews).
        """
        return await self.review(monitor, pr, resume=False)

    @abstractmethod
    async def implement(
        self, monitor: "PipelineMonitor", issue: "IssueRef", *, resume: bool = False
    ) -> WorkOutcome: ...

    @abstractmethod
    async def review(
        self, monitor: "PipelineMonitor", pr: "PrRef", *, resume: bool = False
    ) -> WorkOutcome: ...

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
    ) -> WorkOutcome: ...


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
        return WorkOutcome(
            ok=result.ok, text=result.stdout, error=result.stderr.strip()
        )

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
            monitor.config.repo,
            issue.number,
            issue.title,
            issue.body,
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
            monitor.config.repo,
            pr.number,
            pr.title,
            forge=monitor.forge.config,
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
            monitor.config.repo,
            ref,
            trigger_types or [],
            all_triggers or [],
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


# --- Issue #347 follow-up: smart review resume helpers -----------------------
#
# Reviewer detecta REQUEST_CHANGES / BLOCKED no veredict (subprocess exit
# pode ser rc=0 mesmo assim — `ok` é run-success, não product-success).
# Estes helpers permitem o pipeline distinguir entre "review terminou bem
# (merge/approve)" e "review terminou bloqueando (changes requested)" pra
# preservar o link de resume no DispatchLedger.

_BLOCKED_VERDICT_RE = re.compile(
    r"STATUS:\s*(REQUEST_CHANGES|BLOCKED\w*)",
    re.IGNORECASE,
)


def _review_was_blocked(text: str) -> bool:
    """True se o veredict do reviewer indica que o operador precisa agir
    antes do PR seguir (REQUEST_CHANGES ou BLOCKED_*). Subprocess rc=0
    + STATUS: REQUEST_CHANGES é a combinação típica.

    Conservador: requer match exato pra evitar false positives em prosa
    do reviewer (ex: "vou avaliar se isso bloqueia"). Sem match → False.
    """
    if not text:
        return False
    return bool(_BLOCKED_VERDICT_RE.search(text[-8000:]))


def _worker_kind_from_url(url: str) -> str:
    """Deriva o ``worker_kind`` (telemetria de custo) do endpoint do dispatch.

    Os dispatchers são canônicos ``<kind>-worker`` e cada um responde num
    endpoint ``http://<kind>-worker:<porta>`` (ver ``dispatch_resolver``). O
    ``worker_kind`` gravado no ledger é o ``<kind>`` — extraído do hostname
    (strip de scheme/porta/path) com o sufixo ``-worker`` removido. Genérico:
    cobre toda a frota CLI (``opencode-worker`` → ``opencode``) sem hardcode de
    kinds. Fallback ``deile`` quando o hostname não casa o padrão.
    """
    m = re.search(r"//([^:/]+)", url or "")
    host = m.group(1) if m else ""
    kind = host.removesuffix("-worker")
    return kind or "deile"


def _estimate_session_tokens_from_jsonl(jsonl_text: str) -> int:
    """Soma usage tokens dos turns do JSONL claude. ``jsonl_text`` é o
    conteúdo bruto do arquivo. Tolerante a malformed lines.

    LEGADO / sem call-site de produção: a decisão real de promover resume a
    fresh vive no worker (``claude_worker_server._estimate_context_tokens``),
    que mede o CONTEXTO OCUPADO (pico de um round) — não a SOMA dos rounds.
    Somar ``cache_read_input_tokens`` superestima em ordens de magnitude (o
    mesmo contexto é relido a cada turno). NÃO reative esta função para gate
    de resume sem antes trocar a soma pelo pico (ver issue #445 follow-up).
    """
    total = 0
    for line in (jsonl_text or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        msg = d.get("message") if isinstance(d, dict) else None
        usage = (msg or {}).get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            usage = d.get("usage") if isinstance(d, dict) else None
        if not isinstance(usage, dict):
            continue
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            v = usage.get(k)
            if isinstance(v, (int, float)):
                total += int(v)
    return total


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
            ok=True,
            text=text,
            error="",
            task_id=task_id,
            session_id=session_id,
            **fields,
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
        ok=False,
        text=text,
        error=err[:500],
        task_id=task_id,
        session_id=session_id,
        **fields,
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

    #: Buffer (segundos) somado ao budget HTTP do worker para derivar o teto do
    #: watchdog de tick (:meth:`_post_dispatch`). Pequeno e positivo de propósito:
    #: o ``asyncio.wait_for`` no nível do tick é o HARD-STOP que garante que um
    #: dispatch pendurado NUNCA congela o monitor (regressão de produção
    #: 2026-06-01: o tick travou ~52min num único dispatch que não retornava).
    #: O buffer dá uma janela para o timeout interno do httpx
    #: (``MAX_DISPATCH_BUDGET_S``) disparar primeiro e produzir o
    #: ``WORKER_TIMEOUT`` mais informativo; só se ESSE não disparar (socket
    #: pendurado abaixo do read-timeout, bug do transport) é que o watchdog
    #: assume e converte o hang num ``WorkerDispatchError`` recuperável.
    _TICK_WATCHDOG_BUFFER_S: float = 120.0

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
            from deile.infrastructure.deile_worker_client import DeileWorkerClient

            client = DeileWorkerClient()
        self._client = client
        self._endpoint_override = endpoint_override
        if ledger is None:
            from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

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

        HARD-STOP do tick (regressão de produção 2026-06-01): dispatches
        ``wait=True`` (review/resume) bloqueiam o tick inteiro até o resultado.
        Se o socket ficar pendurado, o tick congelaria por até o budget de 2h —
        a liveness probe não detecta (o status server segue saudável). Aqui
        envolvemos cada dispatch bloqueante num ``asyncio.wait_for`` cujo teto é
        o budget HTTP do worker + um buffer curto, de modo que a chamada SEMPRE
        retorne (sucesso, erro tipado, ou timeout) e o tick prossiga.
        ``asyncio.CancelledError`` é re-levantado (shutdown do monitor) —
        nunca silenciado.
        """
        sig = inspect.signature(self._client.dispatch)
        if "endpoint_url" in sig.parameters:
            coro = self._client.dispatch(payload, wait=wait, endpoint_url=url)
        else:
            coro = self._client.dispatch(payload, wait=wait)

        if not wait:
            # Fire-and-forget já tem timeout curto (``_NOWAIT_TIMEOUT_S``) no
            # client; não precisa de watchdog adicional.
            return await coro

        from deile.infrastructure.deile_worker_client import (
            MAX_DISPATCH_BUDGET_S,
            WorkerDispatchError,
        )

        watchdog_s = MAX_DISPATCH_BUDGET_S + self._TICK_WATCHDOG_BUFFER_S
        try:
            return await asyncio.wait_for(coro, timeout=watchdog_s)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            logger.error(
                "worker dispatch watchdog fired after %.0fs — converting hang "
                "to recoverable failure so the tick proceeds (url=%s)",
                watchdog_s,
                url,
            )
            raise WorkerDispatchError(
                f"worker dispatch exceeded tick watchdog ({watchdog_s:.0f}s) "
                f"and was abandoned to keep the monitor alive",
                error_code="WORKER_TICK_WATCHDOG",
            ) from exc

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
                prev_task_id,
                endpoint_url=url,
            )
        except Exception as exc:  # noqa: BLE001
            # Erro de transporte ou worker — log e fallback pra fresh.
            # Limpa ledger entry pra não consultar resume-info repetidamente.
            from deile.infrastructure.deile_worker_client import WorkerDispatchError

            if isinstance(exc, WorkerDispatchError) and exc.error_code == "NOT_FOUND":
                logger.info(
                    "ledger entry %s aponta task_id=%s sem metadata no worker "
                    "(404) — limpando e fallback fresh",
                    ledger_key,
                    prev_task_id,
                )
                self._ledger.clear(ledger_key)
                return None
            logger.warning(
                "resume-info lookup falhou pra %s task_id=%s: %s — fallback fresh",
                ledger_key,
                prev_task_id,
                exc,
            )
            return None
        if not isinstance(info, dict):
            self._ledger.clear(ledger_key)
            return None
        if not info.get("workdir_exists", False):
            logger.info(
                "ledger entry %s task_id=%s tem workdir perdido — fallback fresh",
                ledger_key,
                prev_task_id,
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
                ledger_key,
                prev_task_id,
                info.get("session_id"),
            )
            return {"_still_alive": True}
        session_id = info.get("session_id") or record.get("session_id")
        if not session_id:
            self._ledger.clear(ledger_key)
            return None
        return {
            "prev_task_id": str(prev_task_id),
            "resume_session_id": str(session_id),
            # Issue #347 follow-up: surface campos extras pro nudge sem
            # uma 2ª chamada HTTP a get_resume_info.
            "_last_result_summary": str(info.get("last_result_summary") or "")[:1500],
            "_last_completed_at": str(info.get("last_completed_at") or ""),
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
        nowait: bool = False,
    ) -> WorkOutcome:
        from deile.infrastructure.deile_worker_client import (
            WorkerDispatchError,
            build_dispatch_payload,
        )

        # Defensive clamp under the 8000-char dispatch cap (issue #257): every
        # body-embedding brief puts the issue/PR body LAST (after the VEREDITO
        # rules), so truncating the tail only trims body context — never the
        # instructions. Guarantees the payload never hard-fails on size.
        if len(brief) > 7950:
            brief = brief[:7950] + "\n…(brief truncado por tamanho)"
        # Decisão #46 — defesa contra HTTP 413 (Request Entity Too Large):
        # mesmo com o cap do brief acima, ``resume_block`` é serializado JSON
        # separadamente e pode crescer (ex: ``pr_url_hint`` longo, futuros
        # campos). Limitamos cada campo string a 50 KiB e o JSON serializado
        # total a 100 KiB. Acima disso, truncamos com sentinela explícita
        # (o worker lê ``.deile-progress.md`` no PASSO 0 para o contexto
        # completo). Sem isso o pipeline rejeita silenciosamente um payload
        # válido só porque crescesse além do ``client_max_size`` (512 KiB)
        # do worker.
        _RESUME_FIELD_CAP = 50 * 1024  # 50 KiB por campo string
        _RESUME_BLOCK_CAP = 100 * 1024  # 100 KiB JSON total
        if isinstance(resume_block, dict):
            truncated = False
            for _k, _v in list(resume_block.items()):
                if isinstance(_v, str) and len(_v) > _RESUME_FIELD_CAP:
                    resume_block[_k] = (
                        _v[:_RESUME_FIELD_CAP]
                        + " … [TRUNCATED — read .deile-progress.md for full]"
                    )
                    truncated = True
            try:
                _serialized = json.dumps(resume_block)
            except (TypeError, ValueError):
                _serialized = ""
            if len(_serialized) > _RESUME_BLOCK_CAP:
                truncated = True
                # Mantém apenas as chaves canônicas indispensáveis pro worker;
                # detalhes ficam no .deile-progress.md (PASSO 0 do brief).
                resume_block = {
                    "mode": resume_block.get("mode", "fresh"),
                    "repo": resume_block.get("repo", ""),
                    "branch": resume_block.get("branch", ""),
                    "main_branch": resume_block.get("main_branch", ""),
                    "expect_merge": resume_block.get("expect_merge", False),
                    "pr_url_hint": str(resume_block.get("pr_url_hint", ""))[:512],
                    "_truncated": (
                        "resume_block excedeu cap de 100 KiB; veja "
                        ".deile-progress.md para detalhes completos"
                    ),
                }
            if truncated:
                logger.warning(
                    "resume_block truncado por cap de tamanho — "
                    "channel_id=%s stage=%s",
                    channel_id,
                    stage,
                )
        # Roteamento do campo de modelo pelo dispatcher do stage. ``stage`` é o
        # nome canônico (ver :data:`PIPELINE_STAGES`); sem override configurado,
        # os resolvers devolvem ``None`` (o worker usa seu próprio default).
        #   - ``deile-worker``/``claude-worker`` (núcleo) consomem
        #     ``preferred_model`` no formato ``provider:model`` (issue #305).
        #   - workers da frota CLI (``*-worker``) consomem ``cli_model`` — id
        #     nativo do CLI, string livre.
        # Esta é a ÚNICA ramificação nova no cliente HTTP: escolher qual campo de
        # modelo preencher conforme o destino, sem relaxar o validator
        # ``provider:model`` do deile-worker.
        is_cli_worker = (
            bool(stage) and resolve_stage_dispatcher(stage) not in BUILTIN_DISPATCHERS
        )
        if is_cli_worker:
            preferred_model = None
            cli_model = resolve_stage_cli_model(stage)
        else:
            preferred_model = resolve_stage_model(stage) if stage else None
            cli_model = None
        # Per-stage reasoning effort (espelha o per-stage model). ``None`` quando
        # nem a etapa nem o global têm override — o worker/provider usa o default.
        preferred_reasoning = resolve_stage_reasoning(stage) if stage else None
        # Per-stage endpoint routing (issue #309 fase 2). ``stage`` opcional
        # mantém compat com callers (testes) que ainda não declaram stage.
        url = self._resolve_endpoint(stage or "implement")

        # RESUME SOB DEMANDA (Decisão #46): fresh dispatch é o default.
        # Resume só é tentado quando o caller passa ``resume=True`` E o
        # ledger tem entry preservada. O brief unificado já lê
        # ``.deile-progress.md`` no PASSO 0 e descobre o estado real da
        # PR/issue, então fresh-com-contexto-natural funciona perfeitamente
        # na maioria dos casos. Esse comportamento evita o crescimento
        # ilimitado do JSONL da sessão claude (visto 11M tokens em produção
        # antes deste fix).
        #
        # Mesmo em ``resume=True``, ainda checamos o ledger; se a entry
        # estiver ausente ou stale, caímos para fresh — não há regressão.
        resume_meta: Optional[Dict[str, str]] = None
        if resume and ledger_key:
            resume_meta = await self._resolve_resume_meta(ledger_key, url)
            if resume_meta and resume_meta.get("_still_alive"):
                # Worker confirmou claude ainda alive — não dispatch.
                # Devolve outcome "em curso" pra stage handler decidir
                # (mantém em_andamento, próximo tick re-checa).
                return WorkOutcome(
                    ok=False,
                    text="",
                    error="DISPATCH_SKIPPED_STILL_RUNNING: claude-worker "
                    "ainda rodando o task anterior; skip nesse tick",
                )

        # Brief contextual quando estamos retomando uma review (não-implementa-
        # ção). O nudge inclui git log delta + comentários novos + checklist
        # de achados anteriores — economiza ~80% dos tokens vs fresh refazendo.
        if resume_meta and stage == "pr_review":
            brief = await self._wrap_review_brief_for_resume(
                brief=brief,
                ledger_key=ledger_key,
                resume_meta=resume_meta,
                url=url,
            )

        # ``stage=stage`` propaga o stage canônico pro worker (issue #309
        # fase 2 hotfix): SEM isso o claude_worker_server caia no default
        # ``implement`` para TODOS os dispatches (review/refine/follow_ups
        # eram todos registrados como implement, quebrando o preamble do
        # pr_review e enganando telemetry).
        # ``timeout_s`` / ``max_retries`` são resolvidos per-stage (issue #391)
        # para dar ao operator controle granular sem editar manifests.
        payload_kwargs: Dict[str, Any] = dict(
            brief=brief,
            channel_id=channel_id,
            persona=persona,
            wait=not nowait,
            preferred_model=preferred_model,
            cli_model=cli_model,
            stage=stage,
            branch=branch,
            preferred_reasoning=preferred_reasoning,
            timeout_s=resolve_stage_timeout_s(stage) if stage else None,
            max_retries=resolve_stage_max_retries(stage) if stage else None,
        )
        if resume_meta:
            # Apenas os 2 campos públicos vão pro wire (resto é interno: _*).
            payload_kwargs["resume_session_id"] = resume_meta["resume_session_id"]
            payload_kwargs["prev_task_id"] = resume_meta["prev_task_id"]
        payload = build_dispatch_payload(**payload_kwargs)
        # The resume context (issue #254) is an additive wire field consumed by
        # the worker; ``build_dispatch_payload`` validates the core fields, so
        # we attach ``resume`` after building to keep that contract untouched.
        if resume_block:
            payload["resume"] = resume_block
        # Issue #392: per-stage cost cap check before POST.
        # Estimate run cost from UsageRepository history + model pricing;
        # raise StageCostCapExceeded when over the cap. None cap = pass-through.
        if stage:
            try:
                from deile.orchestration.pipeline.cost_estimator import (  # noqa: PLC0415
                    StageCostEstimator,
                )
                from deile.storage.usage_repository import (  # noqa: PLC0415
                    StageBudgetGuard,
                    StageCostCapExceeded,
                    get_usage_repository,
                )

                _estimator = StageCostEstimator(
                    usage_repo=get_usage_repository(),
                )
                _guard = StageBudgetGuard(_estimator)
                # Estimate payload tokens from the brief length as a rough proxy
                # (1 token ≈ 4 chars is a conservative heuristic).
                _payload_tokens = max(0, len(brief) // 4)
                _model_for_guard = preferred_model or ""
                _guard.check_stage_run(
                    stage=stage,
                    model_slug=_model_for_guard,
                    payload_size_tokens=_payload_tokens,
                )
            except StageCostCapExceeded as _exc:
                # Escalate: block the dispatch and return a blocked WorkOutcome.
                # The stage handler will see ok=False + motivo_bloqueio prefix
                # and move the issue to ~workflow:bloqueada.
                logger.warning(
                    "cost-cap-exceeded: stage=%s model=%s "
                    "estimated=$%s > cap=$%s — blocking dispatch",
                    _exc.stage,
                    preferred_model,
                    _exc.estimated_usd,
                    _exc.cap_usd,
                )
                return WorkOutcome(
                    ok=False,
                    text="",
                    error=(
                        f"cost-cap-exceeded: estimated USD {_exc.estimated_usd} "
                        f"> cap USD {_exc.cap_usd} for stage {_exc.stage}"
                    ),
                    motivo_bloqueio=(
                        f"cost-cap-exceeded: estimated USD {_exc.estimated_usd} "
                        f"> cap USD {_exc.cap_usd}"
                    ),
                )
            except Exception as _cap_exc:  # noqa: BLE001 — guard never crashes dispatch
                logger.debug(
                    "cost cap check for stage=%s failed (non-fatal): %s",
                    stage,
                    _cap_exc,
                )

        # Scale-to-zero on-demand (plano B5): os CLI workers da frota nascem
        # ``replicas: 0``. Antes de despachar para um deles, garantimos ≥1
        # réplica (kubectl scale via SA do pipeline, com cooldown anti-flapping).
        # Workers núcleo (deile/claude) nascem com 1 réplica → NOT_APPLICABLE.
        # Falha de scale (sem kubectl/RBAC) vira erro tipado WORKER_SCALED_TO_ZERO
        # instruindo o scale manual — em vez do connection-refused genérico.
        if is_cli_worker:
            from deile.orchestration.pipeline.cli_worker_scaler import (  # noqa: PLC0415
                ensure_replica,
            )

            dispatcher = resolve_stage_dispatcher(stage)
            scale_outcome = await ensure_replica(dispatcher)
            if not scale_outcome.ok_to_dispatch:
                logger.warning(
                    "dispatch bloqueado — %s sem réplica e scale falhou: %s",
                    dispatcher,
                    scale_outcome.detail,
                )
                return WorkOutcome(
                    ok=False,
                    text="",
                    error=f"WORKER_SCALED_TO_ZERO: {scale_outcome.detail}"[:500],
                )
            if scale_outcome.result.value in ("scaled", "cooldown"):
                # Pod subindo (cold-start): pula este tick; o reconcile do
                # próximo tick redispatcha quando o readinessProbe liberar.
                logger.info(
                    "dispatch adiado — %s subindo on-demand: %s",
                    dispatcher,
                    scale_outcome.detail,
                )
                return WorkOutcome(
                    ok=False,
                    text="",
                    error=f"DISPATCH_DEFERRED_WORKER_STARTING: {scale_outcome.detail}",
                )

        # Issue #373: ``nowait`` dispatches the task fire-and-forget — the
        # worker returns 202 + task_id immediately and processes the task
        # in the background. The pipeline does NOT block on the result;
        # instead it reconciles ground truth (GitHub labels / PR existence)
        # on the next tick. Used by the implement stage so that N workers
        # can process N issues in parallel.

        # Issue #457 — D1: open parent span `pipeline.dispatch_request` so
        # that the deile.dispatch span opened on the worker side becomes a
        # child (W3C traceparent propagation). Fails open — if the OTel API
        # is not installed, span_ctx stays None and the dispatch proceeds
        # normally without instrumentation. The span covers _post_dispatch()
        # through the final return so traceparent is already injected (D2)
        # before the HTTP POST fires.
        _dispatch_span = None
        _dispatch_ctx_token = None
        try:
            from opentelemetry import context as _otel_context  # noqa: PLC0415
            from opentelemetry import trace as _otel_trace

            _dispatch_span = _otel_trace.get_tracer("deile.pipeline").start_span(
                "pipeline.dispatch_request"
            )
            _dispatch_ctx_token = _otel_context.attach(
                _otel_trace.set_span_in_context(_dispatch_span)
            )
        except ImportError:
            pass

        try:
            wait = not nowait
            try:
                data = await self._post_dispatch(url, payload, wait=wait)
            except WorkerDispatchError as exc:
                # Defense-in-depth contra triple-dispatch (fix 2026-05-27).
                # Worker recusa com 409 quando há claude vivo na mesma sessão
                # (detectado via /proc local OU mtime do JSONL na PVC compartilhada
                # entre réplicas). Trata exatamente como ``_still_alive`` acima:
                # mantém em_andamento, próximo tick re-tenta — NÃO escala
                # como erro porque não é falha do worker, é deduplicação correta.
                if exc.error_code == "CONCURRENT_DISPATCH_BLOCKED":
                    logger.info(
                        "dispatch skipped — worker reportou claude vivo na sessão "
                        "anterior (CONCURRENT_DISPATCH_BLOCKED); aguarda próximo "
                        "tick. ledger_key=%s",
                        ledger_key,
                    )
                    return WorkOutcome(
                        ok=False,
                        text="",
                        error="DISPATCH_SKIPPED_CONCURRENT: claude-worker já tem "
                        "sessão ativa pro mesmo task; skip nesse tick",
                    )
                # Mecanismo 2 (lease): worker recusa com 409 + TASK_ALREADY_RUNNING
                # quando outra réplica detém o lease do workspace desta task.
                # Comportamento idêntico ao CONCURRENT_DISPATCH_BLOCKED: pipeline
                # aguarda o próximo tick, NÃO incrementa tentativas, NÃO limpa
                # o ledger — a task está em andamento em outro pod.
                if exc.error_code == "TASK_ALREADY_RUNNING":
                    logger.info(
                        "dispatch skipped — workspace com lease ativo em outra "
                        "réplica (TASK_ALREADY_RUNNING); aguarda próximo tick. "
                        "ledger_key=%s",
                        ledger_key,
                    )
                    return WorkOutcome(
                        ok=False,
                        text="",
                        error="DISPATCH_SKIPPED_LEASE: workspace desta task está "
                        "com lease ativo em outro pod; skip nesse tick",
                    )
                return WorkOutcome(
                    ok=False, text="", error=f"{exc.error_code}: {exc}"[:500]
                )
            except Exception as exc:  # noqa: BLE001 — never crash the tick
                logger.exception("worker dispatch raised")
                return WorkOutcome(
                    ok=False, text="", error=f"{type(exc).__name__}: {exc}"[:500]
                )
            # Issue #373: fire-and-forget path — worker returned 202 + task_id.
            # The task is running in the background; the pipeline reconciles
            # ground truth on the next tick via ``reconcile_implementing_issues``.
            if nowait:
                task_id = data.get("task_id", "") if isinstance(data, dict) else ""
                logger.info(
                    "worker fire-and-forget accepted: task_id=%s stage=%s",
                    task_id,
                    stage,
                )
                # Grava no ledger também no caminho nowait — critique/refine/review
                # fresh passam ledger_key e precisam de rastreabilidade igual ao
                # implement. Sem isso o task_id se perde antes do reconcile poder
                # fazer resume na próxima janela.
                if ledger_key and task_id:
                    worker_kind = _worker_kind_from_url(url)
                    self._ledger.record(
                        ledger_key,
                        task_id=task_id,
                        session_id="",  # Desconhecido até o worker terminar
                        stage=stage,
                        branch=branch,
                        worker_kind=worker_kind,
                    )
                return WorkOutcome(
                    ok=True,
                    text="",
                    task_id=task_id,
                    ended="",  # Not yet known — reconcile via ground truth
                )
            outcome = _outcome_from_worker_response(data)

            # Observabilidade de custo central (issue #638): persiste 1 registro por
            # modelo no UsageRepository central a partir do bloco ``usage`` estruturado
            # que o cli-worker reporta. Só para workers da frota CLI — deile/claude
            # contabilizam por outras vias (deile grava no SQLite do próprio pod;
            # claude via JSONL). A escrita é best-effort isolada (record_fleet_usage
            # nunca propaga exceção): falha de SQLite/preço NÃO derruba o dispatch.
            if is_cli_worker:
                from deile.orchestration.pipeline.fleet_cost_recorder import (  # noqa: PLC0415
                    record_fleet_usage,
                )

                record_fleet_usage(
                    data,
                    worker_kind=_worker_kind_from_url(url),
                    stage=stage,
                    channel_id=channel_id,
                    cli_model=cli_model,
                )

            # Auth (issue #603): com o token de ~1 ano via setup-token
            # (CLAUDE_CODE_OAUTH_TOKEN) não há refresh in-pod a tentar — quando
            # o worker reporta WORKER_AUTH_EXPIRED, o erro é propagado direto
            # pro stage handler (que bloqueia a issue). O Humano renova com
            # ``deploy.py k8s claude-setup-token``.

            # Issue #309 fase 3.5 + issue #347 follow-up — persistência no
            # DispatchLedger:
            #
            # • ok=True E NÃO houve REQUEST_CHANGES/BLOCKED no veredict:
            #   trabalho concluído (merge, approve, implement bem-sucedido).
            #   → CLEAR ledger; próximo dispatch desse PR/issue é fresh.
            #
            # • ok=True MAS reviewer reportou REQUEST_CHANGES ou BLOCKED_*:
            #   subprocess rodou ok, mas funcionalmente está bloqueado
            #   esperando operador agir. → PRESERVE ledger pra próximo
            #   dispatch poder retomar via --resume com o contexto do reviewer.
            #
            # • ok=False (erro, timeout, etc.): grava entrada pra retry com
            #   resume no próximo tick.
            if ledger_key and outcome.task_id:
                worker_kind = _worker_kind_from_url(url)
                blocked_by_verdict = (
                    stage == "pr_review"
                    and outcome.ok
                    and _review_was_blocked(outcome.text)
                )
                if outcome.ok and not blocked_by_verdict:
                    self._ledger.clear(ledger_key)
                else:
                    self._ledger.record(
                        ledger_key,
                        task_id=outcome.task_id,
                        session_id=outcome.session_id,
                        stage=stage,
                        branch=branch,
                        worker_kind=worker_kind,
                    )
                    if blocked_by_verdict:
                        logger.info(
                            "ledger %s: review REQUEST_CHANGES/BLOCKED — "
                            "preservando entry pra resume da próxima review",
                            ledger_key,
                        )
            return outcome
        finally:
            # Issue #457 — close pipeline.dispatch_request span on all exit
            # paths (return / exception). The context token is detached so
            # the parent span is restored in the asyncio task context.
            if _dispatch_span is not None:
                _dispatch_span.end()
            if _dispatch_ctx_token is not None:
                try:
                    from opentelemetry import context as _otel_context  # noqa: PLC0415

                    _otel_context.detach(_dispatch_ctx_token)
                except ImportError:
                    pass

    async def _wrap_review_brief_for_resume(
        self,
        *,
        brief: str,
        ledger_key: str,
        resume_meta: Dict[str, str],
        url: str,
    ) -> str:
        """Constrói o nudge contextual rico que substitui o brief original
        em dispatches de RESUME de pr_review.

        Inclui: prev verdict + git delta entre HEAD anterior e HEAD atual +
        comentários novos no PR desde a última review + instruções
        explícitas pra NÃO repetir trabalho.

        ``brief`` original (do _render_worker_pr_unified_brief) é DESCARTADO
        nesse caminho — claude já viu ele na sessão anterior via -r.
        O nudge é minimalista e direcional.

        Best-effort: erros em qualquer fetch caem pro nudge mínimo (sem
        delta details). Resume continua funcional.
        """
        _prev_task_id = resume_meta["prev_task_id"]
        session_id = resume_meta["resume_session_id"]
        # Extrai pr_number do ledger_key formato "pr:<N>".
        pr_number = None
        if ledger_key and ledger_key.startswith("pr:"):
            try:
                pr_number = int(ledger_key.split(":", 1)[1])
            except (ValueError, IndexError):
                pass

        # Usa info já obtido pelo _resolve_resume_meta (evita 2ª chamada HTTP).
        prev_summary = resume_meta.get("_last_result_summary") or ""
        try:
            prev_completed_at = int(resume_meta.get("_last_completed_at") or 0)
        except (ValueError, TypeError):
            prev_completed_at = 0

        # Coleta delta (git log + git diff + gh comments) — TODOS best-effort,
        # cada chamada protegida individualmente pra não derrubar o brief.
        delta_block = ""
        if pr_number is not None:
            delta_block = await self._collect_review_delta(pr_number, prev_completed_at)

        nudge_lines = [
            f"# RESUME DE REVIEW — PR #{pr_number or '?'} (sessão claude --resume {session_id[:8]}…)",
            "",
            "Você JÁ revisou esta PR antes. Sua sessão claude foi RETOMADA via `-r`,",
            "então você tem TODO o contexto preservado: leituras, achados, decisões.",
            "",
            "## VEREDICT ANTERIOR (resumo persistido pelo worker)",
            "",
            prev_summary
            or "(sumário anterior indisponível; reconstrua pelo histórico da sessão)",
            "",
        ]
        if delta_block:
            nudge_lines.append(delta_block)
        nudge_lines.extend(
            [
                "## INSTRUÇÕES — siga EXATAMENTE",
                "",
                "1. **NÃO releia o repositório inteiro.** Você já leu na sessão anterior.",
                "   `cat`/`Read` APENAS arquivos tocados pelo delta abaixo.",
                "2. **NÃO rode a suite completa de testes** (gasta 10+ min). Identifique",
                "   o subset afetado pelo delta e rode SÓ ele (ex: `pytest deile/tests/<X>/`).",
                "   Suite full só se o delta toca código central com fan-in alto.",
                "3. **Para cada item da sua review anterior, marque o status novo:**",
                "   - ✓ resolvido (correção aplicada e validada)",
                "   - ✗ ainda aberto (correção não aplicada ou regrediu)",
                "   - ⚠ parcial (resolveu parte mas introduziu outro problema)",
                "4. **Avalie comentários novos** (se houver acima) — operador pode ter",
                "   esclarecido escopo ou pedido ajuste adicional.",
                "5. **Re-emita seu veredict NOVO** depois de POSTAR comment no PR via",
                "   `gh pr review` ou `gh issue comment`:",
                "   - STATUS: APPROVE — todos achados resolvidos + delta limpo → vai mergear",
                "   - STATUS: REQUEST_CHANGES — ainda há achados abertos OU delta",
                "     introduziu problema novo (liste explicitamente)",
                "   - STATUS: BLOCKED_<motivo> — impedimento estrutural que operador não",
                "     pode corrigir só com push (ex: regressão de teste pré-existente)",
                "6. **Se for APPROVE**: faça merge via `gh pr merge --squash` (ou --merge",
                "   se a PR exigir merge-commit). Confirme o merge antes de imprimir",
                "   STATUS final.",
                "",
                "Sem refazer trabalho redundante. O delta é tudo que mudou.",
            ]
        )
        return "\n".join(nudge_lines)

    async def _collect_review_delta(
        self,
        pr_number: int,
        prev_completed_at: int,
    ) -> str:
        """Constrói o bloco de delta (git log + diff + comments) pro nudge
        de resume. Best-effort; erros viram bloco vazio.

        Roda LOCAL no pipeline pod via subprocess.run — `gh` está no
        PATH; `git` está no PATH (clone de pipeline-status existe).
        """
        import asyncio as _aio
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        lines: List[str] = []
        # gh comments since prev_completed_at.
        if prev_completed_at:
            since_iso = _dt.fromtimestamp(prev_completed_at, _tz.utc).isoformat()
            try:
                proc = await _aio.create_subprocess_exec(
                    "gh",
                    "api",
                    f"repos/{resolve_forge_repo()}/issues/{pr_number}/comments",
                    "--paginate",
                    "-q",
                    f'.[] | select(.created_at > "{since_iso}") | '
                    '"[\\(.created_at[11:19])Z \\(.user.login)] \\(.body[:300])"',
                    stdout=_aio.subprocess.PIPE,
                    stderr=_aio.subprocess.DEVNULL,
                )
                out, _ = await _aio.wait_for(proc.communicate(), timeout=10)
                comments_text = (out or b"").decode("utf-8", "replace").strip()
                if comments_text:
                    lines.append("## COMENTÁRIOS NOVOS na PR (desde sua última review)")
                    lines.append("")
                    lines.append(comments_text[:2000])
                    lines.append("")
            except Exception:  # noqa: BLE001
                pass
        # Push delta via gh — listar últimos N commits da PR como contexto.
        try:
            proc = await _aio.create_subprocess_exec(
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "commits",
                "-q",
                ".commits | sort_by(.committedDate) | reverse | .[0:5] | "
                '.[] | "  \\(.oid[0:8]) \\(.messageHeadline)"',
                stdout=_aio.subprocess.PIPE,
                stderr=_aio.subprocess.DEVNULL,
            )
            out, _ = await _aio.wait_for(proc.communicate(), timeout=10)
            log_text = (out or b"").decode("utf-8", "replace").strip()
            if log_text:
                lines.append("## COMMITS RECENTES na PR (últimos 5)")
                lines.append("```")
                lines.append(log_text[:1500])
                lines.append("```")
                lines.append("")
                lines.append(
                    "Use `git log --oneline -10 origin/<branch>` no workdir reaproveitado"
                )
                lines.append(
                    "e `git diff <commit_anterior>..HEAD` pra ver o delta REAL."
                )
                lines.append("")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)

    async def implement(
        self, monitor: "PipelineMonitor", issue: "IssueRef", *, resume: bool = False
    ) -> WorkOutcome:
        branch = monitor.branch_for_issue(issue.number)
        forge_cfg = monitor.forge.config
        render = (
            _render_worker_implement_resume_brief
            if resume
            else _render_worker_implement_brief
        )
        brief = render(
            monitor.config.repo,
            monitor.config.main_branch,
            branch,
            issue.number,
            issue.title,
            issue.body,
            forge=forge_cfg,
        )
        resume_block = _build_resume_block(
            monitor.config.repo,
            monitor.config.main_branch,
            branch,
            resume=resume,
            expect_merge=False,
        )
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

        # Issue #373: fire-and-forget for FRESH dispatches — the pipeline no
        # longer blocks waiting for the worker to finish. Resume dispatches
        # still block because the stage handler needs the structured result
        # (ended, fingerprint, tentativa) to decide concluido/incompleto/bloqueado.
        return await self._dispatch(
            brief,
            channel_id=f"pipeline-issue-{issue.number}",
            resume_block=resume_block,
            stage="implement",
            branch=branch,
            ledger_key=DispatchLedger.key_for_issue(issue.number),
            resume=resume,
            nowait=not resume,
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
            monitor.config.repo,
            issue.number,
            issue.title,
            issue.body,
            issue_type=issue_type or "",
            template=template_for_type(issue_type) or "intent.md",
            forge=monitor.forge.config,
        )
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

        # A crítica (julgar CLARO/VAGO) é o PRIMEIRO passo LLM e roteia pelo stage
        # ``classify`` — distinto do ``refine`` (reescrever o corpo). São duas
        # chamadas LLM separadas em ticks distintos (briefs distintos), então
        # cada uma pode ter worker/modelo próprios: classify→juízo barato,
        # refine→reescrita com modelo melhor. O ``classify_new_issues`` em
        # ``stages.py`` é só a admissão Python (label ~workflow:nova), não LLM.
        return await self._dispatch(
            brief,
            channel_id=f"pipeline-issue-{issue.number}",
            persona=persona_for_type(issue_type),
            stage="classify",
            ledger_key=DispatchLedger.key_for_issue(issue.number),
            nowait=True,
        )

    async def refine(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        issue_type = issue_type_from_labels(issue.labels)
        brief = _render_worker_refine_brief(
            monitor.config.repo,
            issue.number,
            issue.title,
            issue.body,
            issue_type=issue_type or "",
            template=template_for_type(issue_type) or "intent.md",
            forge=monitor.forge.config,
        )
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

        return await self._dispatch(
            brief,
            channel_id=f"pipeline-issue-{issue.number}",
            persona=persona_for_type(issue_type),
            stage="refine",
            ledger_key=DispatchLedger.key_for_issue(issue.number),
            nowait=True,
        )

    async def decompose(
        self, monitor: "PipelineMonitor", issue: "IssueRef"
    ) -> WorkOutcome:
        brief = _render_worker_decompose_brief(
            monitor.config.repo,
            issue.number,
            issue.title,
            issue.body,
            forge=monitor.forge.config,
        )
        return await self._dispatch(
            brief,
            channel_id=f"pipeline-issue-{issue.number}",
            persona="architect",
            stage="refine",
        )

    async def review(
        self, monitor: "PipelineMonitor", pr: "PrRef", *, resume: bool = False
    ) -> WorkOutcome:
        forge_cfg = monitor.forge.config
        # Refactor "PR é o quadro": revisão de PR também usa o brief unificado.
        # O worker descobre o estado real (HEAD vs último review, threads
        # abertas) e age conforme — inclusive sabe lidar com resume lendo
        # ``.deile-progress.md`` no passo 0 do brief.
        gh_login = monitor.config.mention_handle.lstrip("@")
        brief = _render_worker_pr_unified_brief(
            monitor.config.repo,
            monitor.config.main_branch,
            pr.number,
            gh_login=gh_login,
            forge=forge_cfg,
        )
        resume_block = _build_resume_block(
            monitor.config.repo,
            monitor.config.main_branch,
            pr.head_ref or f"pr/{pr.number}",
            resume=resume,
            expect_merge=True,
            pr_url_hint=pr.url,
        )
        # The review/merge stage is the final quality gate: dispatch under the
        # ``reviewer`` persona (instructions in personas/instructions/reviewer.md)
        # so the worker evaluates SOLID/SRP/DRY/KISS/security/idempotency, not
        # just whether the suite is green. implement/mention keep ``developer``.
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

        # Issue #373 (espelhando implement): dispatch fresh fire-and-forget para
        # não bloquear o tick. Resume permanece bloqueante — o stage handler de
        # pr_review precisa do resultado estruturado (ended, fingerprint,
        # tentativa) para decidir concluido/incompleto/bloqueado.
        return await self._dispatch(
            brief,
            channel_id=f"pipeline-pr-{pr.number}",
            persona="reviewer",
            resume_block=resume_block,
            stage="pr_review",
            branch=pr.head_ref or f"pr/{pr.number}",
            ledger_key=DispatchLedger.key_for_pr(pr.number),
            resume=resume,
            nowait=not resume,
        )

    async def address_review(
        self, monitor: "PipelineMonitor", pr: "PrRef"
    ) -> WorkOutcome:
        """Despacha um IMPLEMENT que aplica o feedback do reviewer + push (Fix #8).

        Diferente de :meth:`review`, este dispatch NÃO revisa nem mergeia — manda
        o worker LER a última review REQUEST_CHANGES, escrever o código que
        atende e dar push na branch da própria PR. Quando o push muda o HEAD, a
        próxima review (caminho normal) valida o novo HEAD e segue pro merge.

        Roteado como ``stage="implement"`` (escreve código, não revisa) e
        fire-and-forget (``nowait=True``, como o implement fresh): o tick não
        bloqueia esperando o worker terminar — o resultado é observado no tick
        seguinte pela mudança do HEAD SHA. Compartilha o ``channel_id`` do
        pr_review para reaproveitar o workspace já com a PR clonada.
        """
        forge_cfg = monitor.forge.config
        branch = pr.head_ref or f"pr/{pr.number}"
        brief = _render_worker_pr_address_brief(
            monitor.config.repo,
            monitor.config.main_branch,
            branch,
            pr.number,
            forge=forge_cfg,
        )
        resume_block = _build_resume_block(
            monitor.config.repo,
            monitor.config.main_branch,
            branch,
            resume=False,
            expect_merge=False,
            pr_url_hint=pr.url,
        )
        from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

        return await self._dispatch(
            brief,
            channel_id=f"pipeline-pr-{pr.number}",
            persona="developer",
            resume_block=resume_block,
            stage="implement",
            branch=branch,
            ledger_key=DispatchLedger.key_for_pr(pr.number),
            nowait=True,
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
        """Dispatch a mention/assignment para o brief unificado de PR ou para o
        brief de mention-em-issue (refactor "PR é o quadro").

        Apenas dois modes existem agora:

        - ``pr_unified`` — qualquer trigger sobre uma PR (assignee, reviewer,
          comment, body). O worker descobre o estado real e age conforme. Não
          recebe parâmetro ``expect_merge`` — o brief decide se mergeia (apenas
          quando autor sou eu E review APPROVED E threads ok E CI verde).
        - ``comment`` — comment mention sobre uma issue. Mantém o brief
          context-rich tradicional sob a persona ``developer``.
        """
        repo = monitor.config.repo
        main = monitor.config.main_branch
        number = ref.target_number
        channel_id = f"pipeline-mention-{ref.target_kind}-{number}"
        pr_ref = next((t.pr for t in (all_triggers or [ref]) if t.pr is not None), None)
        head = (pr_ref.head_ref if pr_ref else "") or f"pr/{number}"
        pr_url_hint = pr_ref.url if pr_ref else ""

        # ForgeConfig do monitor — usado nos briefs forge-aware (issue #297).
        forge_cfg = monitor.forge.config

        # PR scope: brief unificado, persona reviewer, stage pr_review. O worker
        # decide o que fazer (revisar / responder / mergear / no-op) a partir
        # do estado real — o trigger só apontou QUAL PR olhar. ``expect_merge``
        # = True porque o brief PODE mergear (quando todas as pré-condições
        # naturais estiverem cumpridas).
        if ref.target_kind == "pr":
            gh_login = monitor.config.mention_handle.lstrip("@")
            unified_brief = _render_worker_pr_unified_brief(
                repo,
                main,
                number,
                gh_login=gh_login,
                forge=forge_cfg,
            )
            from deile.orchestration.pipeline.dispatch_ledger import DispatchLedger

            return await self._dispatch(
                unified_brief,
                channel_id=channel_id,
                persona="reviewer",
                resume_block=_build_resume_block(
                    repo,
                    main,
                    head,
                    resume=resume,
                    expect_merge=True,
                    pr_url_hint=pr_url_hint,
                ),
                stage="pr_review",
                branch=head,
                # mentions PR-scoped usam mesma chave que pr_review pra que o
                # pipeline reaproveite session se houver resume.
                ledger_key=DispatchLedger.key_for_pr(number),
                resume=resume,
                # FIX #5 (issue #518): dispatch fire-and-forget — o tick NÃO
                # pode ficar preso esperando o claude terminar a PR (até 2h).
                # nowait=True espelha o que pr_review FRESH já faz (issue #373).
                # O estado da PR (labels) reflete o progresso no tick seguinte.
                # Resume usa wait=True (comportamento preservado via nowait=not resume).
                nowait=not resume,
            )
        # Default: comment mention on an issue → do what the comment says.
        brief = _render_worker_mention_brief(
            repo,
            ref,
            trigger_types or [],
            all_triggers or [],
            forge=forge_cfg,
        )
        return await self._dispatch(
            brief,
            channel_id=channel_id,
            persona="developer",
            stage="follow_ups",
        )


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

# ``WORKER_ALIASES`` / ``CLAUDE_ALIASES`` são a fonte única em
# :mod:`deile.orchestration.pipeline.dispatch_resolver` (importados no topo) —
# re-exportados aqui por compatibilidade com importadores que os esperam neste
# módulo. Os nomes underscored mantêm callers internos legados.
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
        from deile.orchestration.pipeline.dispatch_resolver import (  # noqa: PLC0415
            is_valid_dispatcher,
        )

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
