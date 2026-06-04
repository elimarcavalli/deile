"""Pipeline-side resume bookkeeping for the autonomous pipeline (issue #254).

The deile-worker persists the authoritative per-issue state (attempt counter,
accumulated budget, journal) in its PVC workspace and RETURNS the structured
result (``ended``/``fingerprint``/``tentativa``/...) on each dispatch. The
pipeline, however, runs in a different process with no access to that PVC, so it
needs a small amount of its own cross-tick memory to:

- **Enforce cadence** (item 9): remember WHEN it last dispatched a resume for an
  issue so it can honor ``pipeline_resume_interval`` before re-dispatching.
- **Run the progress guard** (item 4): remember the LAST substantive fingerprint
  the worker reported so it can detect "identical between attempts = 0 progress".
- **Enforce the ceiling** (item 6): remember the attempt count and accumulated
  budget reported by the worker.

This is pipeline coordination state attached to the long-lived
:class:`PipelineMonitor` instance (mirroring how it already keeps
``_mention_cursor``), NOT agent memory — it carries no user content, no secrets,
and is intentionally ephemeral (lost on monitor restart; the worker's PVC state
is the durable copy and re-seeds it on the next dispatch).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class IssueResumeState:
    """Per-issue resume state held by the monitor between ticks."""

    #: Wall-clock (monotonic seconds) of the last dispatch for this issue.
    last_dispatch_monotonic: float = 0.0
    #: Last substantive fingerprint the worker reported (progress guard).
    last_fingerprint: str = ""
    #: Attempt counter as reported by the worker (authoritative source: PVC).
    attempt: int = 0
    #: Accumulated wall-clock budget (seconds) reported by the worker.
    budget_s: float = 0.0
    #: Refinement passes already applied to this issue (refinement gate). Unlike
    #: ``attempt`` this has no durable PVC source — it is reset on monitor restart
    #: (a safety ceiling, not a hard guarantee). Bounded by ``refine_max_attempts``.
    refine_attempt: int = 0
    #: Last error signature (e.g. "TIMEOUT", "INCOMPLETO_SEM_PR", "WORKER_UNREACHABLE").
    #: Used to detect 2 consecutive identical failures = same root cause →
    #: pipeline can escalate (block earlier) instead of burning ``resume_max_attempts``
    #: dispatches all hitting the same wall.
    last_error_kind: str = ""
    #: Count of consecutive failures with ``last_error_kind`` (cleared on success).
    same_error_streak: int = 0
    #: Dedicated counter for "agent finished but no PR" failures — this class of
    #: failure tends to be irrecoverable (LLM gave up on the task structure), so
    #: a tighter ceiling (``incomplete_no_pr_max``) makes sense than the generic
    #: ``resume_max_attempts`` used for transient timeouts.
    incomplete_no_pr_count: int = 0
    #: HEAD SHA of the branch at the time of the last review that did NOT merge.
    #: Used by the deterministic re-review flood guard (Fix A): if the HEAD SHA
    #: has not changed since the last incomplete review, no fix was applied and
    #: re-reviewing the same HEAD is a flood — the guard forces zero_progress.
    #: Empty string when forge does not expose SHA (GitLab fallback → guard skips).
    last_reviewed_sha: str = ""
    #: Body length (chars) after the PREVIOUS refine pass. Used by the divergence
    #: early-stop guard (Fix B): if the body keeps growing on the 3rd+ pass the
    #: scope is diverging (each pass only adds gaps) — block early instead of
    #: burning all 5 passes. -1 means "no previous pass recorded yet".
    prev_refine_body_len: int = -1


@dataclass
class ResumeTracker:
    """In-memory map of ``issue_number → IssueResumeState``.

    Lives on the monitor instance. All reads/writes are synchronous and cheap;
    no I/O. Methods are deliberately tiny so the stage logic stays readable.
    """

    _by_issue: Dict[int, IssueResumeState] = field(default_factory=dict)

    def get(self, number: int) -> IssueResumeState:
        """Return (creating if absent) the state for *number*."""
        state = self._by_issue.get(number)
        if state is None:
            state = IssueResumeState()
            self._by_issue[number] = state
        return state

    def peek(self, number: int) -> Optional[IssueResumeState]:
        """Return the state for *number* without creating one."""
        return self._by_issue.get(number)

    def record_dispatch(self, number: int, now_monotonic: float) -> None:
        """Stamp the dispatch time so cadence can be enforced next tick."""
        self.get(number).last_dispatch_monotonic = now_monotonic

    def update_from_worker(
        self,
        number: int,
        *,
        fingerprint: str,
        attempt: int,
        budget_s: float,
    ) -> None:
        """Absorb the worker's structured result into the tracked state.

        ``update_from_worker`` is called exactly once per dispatch outcome, so
        the pipeline-side counter is **always grown by at least 1** here. This
        protects against the worker reporting ``tentativa=1`` every time (which
        happens when the PVC progress file is missing, the workspace was reset,
        or the worker bookkeeping silently fails) — the ceiling in stages.py
        would otherwise never bite and the issue would re-dispatch forever
        (regression observed on #283: 50+ "incompleto sem PR" parks before the
        operator manually applied ``~workflow:bloqueada``). The worker's
        ``attempt`` is honored when LARGER than the pipeline's count (it has
        a durable PVC source we lack), but never used to shrink the counter.
        """
        state = self.get(number)
        if fingerprint:
            state.last_fingerprint = fingerprint
        # Always +1 per call (each call == one dispatch); worker's view wins
        # only when it is AHEAD (legitimate growth from durable bookkeeping).
        state.attempt = max(state.attempt + 1, attempt or 0)
        if budget_s:
            state.budget_s = max(state.budget_s, budget_s)

    def bump_refine(self, number: int) -> int:
        """Incrementa e retorna o contador de passes de refino para *number*."""
        state = self.get(number)
        state.refine_attempt += 1
        return state.refine_attempt

    def refine_attempt(self, number: int) -> int:
        """Retorna os passes de refino já aplicados a *number* (0 se nenhum)."""
        state = self.peek(number)
        return state.refine_attempt if state is not None else 0

    def set_refine_attempt(self, number: int, n: int) -> None:
        """Define o contador de passes de refino se *n* for MAIOR que o atual.

        Usado para reconciliar o estado in-memory com a label durável ``~refine:N``
        após restart do pod. Nunca encolhe o contador — garante monotonicidade.
        """
        state = self.get(number)
        if n > state.refine_attempt:
            state.refine_attempt = n

    def clear(self, number: int) -> None:
        """Drop tracked state for *number* (e.g. once it reaches em_pr/blocked)."""
        self._by_issue.pop(number, None)

    def cadence_ok(self, number: int, now_monotonic: float, interval_s: int) -> bool:
        """True if enough time has elapsed since the last dispatch.

        ``interval_s <= 0`` means "immediate" — always allowed. A first attempt
        (no recorded dispatch) is always allowed.

        **Backoff exponencial** (a partir da 2ª tentativa): a janela efetiva é
        ``interval_s * 2**min(attempt-1, 4)`` — issue saudável retoma na
        cadência configurada; issue problemática espera 2×, 4×, 8×, 16×
        antes de cada retry (teto em 16×). Limita queima de tokens em loops
        difíceis sem precisar bloquear cedo demais.
        """
        if interval_s <= 0:
            return True
        state = self.peek(number)
        if state is None or state.last_dispatch_monotonic <= 0.0:
            return True
        attempt = state.attempt or 0
        backoff_factor = 2 ** min(max(attempt - 1, 0), 4)
        effective_interval = interval_s * backoff_factor
        return (now_monotonic - state.last_dispatch_monotonic) >= effective_interval

    def record_failure(self, number: int, error_kind: str) -> int:
        """Record an outcome failure and return the consecutive-same-error streak.

        ``error_kind`` is a short signature (e.g. ``"TIMEOUT"``,
        ``"INCOMPLETO_SEM_PR"``, ``"WORKER_UNREACHABLE"``). Two consecutive
        failures with the same kind signal a non-transient cause — the caller
        can escalate to ``_block_issue`` instead of burning the full ceiling.
        """
        state = self.get(number)
        if error_kind and error_kind == state.last_error_kind:
            state.same_error_streak += 1
        else:
            state.last_error_kind = error_kind
            state.same_error_streak = 1
        return state.same_error_streak

    def clear_failure(self, number: int) -> None:
        """Reset the failure streak (call on success)."""
        state = self.peek(number)
        if state is not None:
            state.last_error_kind = ""
            state.same_error_streak = 0

    def bump_incomplete_no_pr(self, number: int) -> int:
        """Increment and return the dedicated 'incomplete no PR' counter."""
        state = self.get(number)
        state.incomplete_no_pr_count += 1
        return state.incomplete_no_pr_count

    def record_refine_body_len(self, number: int, body_len: int) -> None:
        """Grava o comprimento do body após o passe de refino corrente.

        Chamado em :func:`_apply_refine_verdict` DEPOIS de confirmar que o body
        mudou (passe não convergiu). O próximo passe vai comparar o seu
        ``after_body`` com este valor para detectar divergência (Fix B).
        """
        self.get(number).prev_refine_body_len = body_len

    def get_prev_refine_body_len(self, number: int) -> int:
        """Retorna o comprimento do body do passe anterior de refino, ou -1."""
        state = self.peek(number)
        return state.prev_refine_body_len if state is not None else -1

    def set_reviewed_sha(self, number: int, sha: str) -> None:
        """Grava o HEAD SHA da última review incompleta para a PR *number*.

        Chamado no caminho "não-merged, será retomada" de :func:`review_one_open_pr`
        para que o próximo tick possa comparar o SHA atual com este e detectar
        se nenhum fix foi aplicado (flood guard da Fix A).
        """
        self.get(number).last_reviewed_sha = sha

    def reviewed_sha(self, number: int) -> str:
        """Retorna o HEAD SHA da última review incompleta para *number* (ou "")."""
        state = self.peek(number)
        return state.last_reviewed_sha if state is not None else ""

    def is_zero_progress(self, number: int, new_fingerprint: str) -> bool:
        """True if *new_fingerprint* equals the last one tracked (progress guard).

        An empty fingerprint (worker could not compute one) is never treated as
        zero progress — we only block on a CONFIRMED identical substantive
        fingerprint, so a missing measurement errs on the side of continuing.
        """
        if not new_fingerprint:
            return False
        state = self.peek(number)
        if state is None or not state.last_fingerprint:
            return False
        return state.last_fingerprint == new_fingerprint
