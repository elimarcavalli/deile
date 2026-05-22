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
        """Absorb the worker's structured result into the tracked state."""
        state = self.get(number)
        if fingerprint:
            state.last_fingerprint = fingerprint
        if attempt:
            state.attempt = attempt
        if budget_s:
            state.budget_s = budget_s

    def clear(self, number: int) -> None:
        """Drop tracked state for *number* (e.g. once it reaches em_pr/blocked)."""
        self._by_issue.pop(number, None)

    def cadence_ok(self, number: int, now_monotonic: float, interval_s: int) -> bool:
        """True if enough time has elapsed since the last dispatch.

        ``interval_s <= 0`` means "immediate" — always allowed. A first attempt
        (no recorded dispatch) is always allowed.
        """
        if interval_s <= 0:
            return True
        state = self.peek(number)
        if state is None or state.last_dispatch_monotonic <= 0.0:
            return True
        return (now_monotonic - state.last_dispatch_monotonic) >= interval_s

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
