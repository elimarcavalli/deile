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

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Tuple

from deile.orchestration.pipeline.briefs import (
    _render_claude_mention_prompt, _render_worker_critique_brief,
    _render_worker_decompose_brief, _render_worker_implement_brief,
    _render_worker_implement_resume_brief, _render_worker_mention_brief,
    _render_worker_pr_address_brief, _render_worker_refine_brief,
    _render_worker_review_brief, _render_worker_review_only_brief,
    _render_worker_review_resume_brief)
from deile.orchestration.pipeline.claude_dispatcher import (
    render_implement_prompt, render_review_prompt)
from deile.orchestration.pipeline.labels import (issue_type_from_labels,
                                                 persona_for_type,
                                                 template_for_type)
from deile.orchestration.pipeline.model_resolver import resolve_stage_model

if TYPE_CHECKING:  # pragma: no cover - typing only
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
    text = str(data.get("summary") or "")
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
    if ok:
        return WorkOutcome(ok=True, text=text, error="", **fields)
    err = str(data.get("error") or data.get("summary") or "worker reported failure")
    return WorkOutcome(ok=False, text=text, error=err[:500], **fields)


class WorkerImplementer(PipelineImplementer):
    """Dispatch implement/review/mention work to the ``deile-worker`` Pod.

    The worker is another DEILE running the full toolset behind an HTTP
    control plane. It clones the repo, branches, implements/reviews, runs
    tests and opens/merges the PR in its own isolated, per-channel workspace.
    The pipeline-side ``channel_id`` is synthetic (``pipeline-issue-<N>`` /
    ``pipeline-pr-<N>``) so each work item gets a stable, reusable sandbox.
    """

    name = "deile_worker"

    def __init__(self, client: Optional[object] = None) -> None:
        if client is None:
            from deile.infrastructure.deile_worker_client import \
                DeileWorkerClient
            client = DeileWorkerClient()
        self._client = client

    async def _dispatch(
        self,
        brief: str,
        *,
        channel_id: str,
        persona: str = "developer",
        resume_block: Optional[dict] = None,
        stage: Optional[str] = None,
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
        payload = build_dispatch_payload(
            brief=brief, channel_id=channel_id, persona=persona, wait=True,
            preferred_model=preferred_model,
        )
        # The resume context (issue #254) is an additive wire field consumed by
        # the worker; ``build_dispatch_payload`` validates the core fields, so
        # we attach ``resume`` after building to keep that contract untouched.
        if resume_block:
            payload["resume"] = resume_block
        try:
            data = await self._client.dispatch(payload, wait=True)
        except WorkerDispatchError as exc:
            return WorkOutcome(ok=False, text="", error=f"{exc.error_code}: {exc}"[:500])
        except Exception as exc:  # noqa: BLE001 — never crash the tick
            logger.exception("worker dispatch raised")
            return WorkOutcome(ok=False, text="", error=f"{type(exc).__name__}: {exc}"[:500])
        return _outcome_from_worker_response(data)

    async def implement(
        self, monitor: "PipelineMonitor", issue: "IssueRef", *, resume: bool = False
    ) -> WorkOutcome:
        branch = monitor.branch_for_issue(issue.number)
        forge_cfg = monitor.forge.config
        if resume:
            brief = _render_worker_implement_resume_brief(
                monitor.config.repo, monitor.config.main_branch, branch,
                issue.number, issue.title, issue.body, forge=forge_cfg,
            )
        else:
            brief = _render_worker_implement_brief(
                monitor.config.repo, monitor.config.main_branch, branch,
                issue.number, issue.title, issue.body, forge=forge_cfg,
            )
        resume_block = _build_resume_block(
            monitor.config.repo, monitor.config.main_branch, branch,
            resume=resume, expect_merge=False,
        )
        return await self._dispatch(
            brief, channel_id=f"pipeline-issue-{issue.number}",
            resume_block=resume_block, stage="implement",
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
        if resume:
            brief = _render_worker_review_resume_brief(
                monitor.config.repo, monitor.config.main_branch, pr.number,
                forge=forge_cfg,
            )
        else:
            brief = _render_worker_review_brief(
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
        return await self._dispatch(
            brief, channel_id=f"pipeline-pr-{pr.number}",
            persona="reviewer", resume_block=resume_block, stage="pr_review",
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
            return await self._dispatch(
                reviewer_brief, channel_id=channel_id, persona="reviewer",
                resume_block=_build_resume_block(
                    repo, main, head, resume=resume, expect_merge=expect_merge,
                    pr_url_hint=pr_url_hint,
                ),
                stage="pr_review",
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
    """
    return (dispatch_mode or "claude").strip().lower() in CLAUDE_ALIASES


def build_implementer(
    dispatch_mode: str, *, worker_client: Optional[object] = None
) -> PipelineImplementer:
    """Return the implementer strategy selected by ``dispatch_mode``.

    ``deile_worker`` (and aliases) → :class:`WorkerImplementer`;
    ``claude`` (and aliases) → :class:`ClaudeImplementer`. An empty/None mode
    defaults to Claude (legacy behaviour). Um valor **não-vazio** que não
    case com nenhum alias dispara :class:`ValueError` em vez de cair em
    Claude silenciosamente — um typo em ``DEILE_PIPELINE_DISPATCH_MODE``
    (ex.: ``deile_woker``) usaria a chave da Anthropic e queimaria budget
    sem o operador perceber. Fail-fast surface o erro imediatamente.
    """
    if not dispatch_mode or not dispatch_mode.strip():
        # Legacy default (vazio/None) — usa Claude.
        return ClaudeImplementer()
    mode = dispatch_mode.strip().lower()
    if mode in _WORKER_ALIASES:
        return WorkerImplementer(client=worker_client)
    if mode in _CLAUDE_ALIASES:
        return ClaudeImplementer()
    raise ValueError(
        f"unknown pipeline dispatch_mode {dispatch_mode!r}; "
        f"expected one of {sorted(_WORKER_ALIASES | _CLAUDE_ALIASES)} "
        "(set DEILE_PIPELINE_DISPATCH_MODE explicitly)"
    )
