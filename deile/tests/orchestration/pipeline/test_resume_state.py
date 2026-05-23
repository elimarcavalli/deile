"""Unit tests for the pipeline-side resume tracker (issue #254)."""

from __future__ import annotations

from deile.orchestration.pipeline.resume_state import ResumeTracker


class TestResumeTracker:
    def test_get_creates_state(self):
        t = ResumeTracker()
        assert t.peek(1) is None
        state = t.get(1)
        assert state is t.get(1)  # same instance on re-fetch
        assert t.peek(1) is state

    def test_record_dispatch_stamps_time(self):
        t = ResumeTracker()
        t.record_dispatch(1, 100.0)
        assert t.get(1).last_dispatch_monotonic == 100.0

    def test_update_from_worker_absorbs_fields(self):
        t = ResumeTracker()
        t.update_from_worker(1, fingerprint="abc", attempt=3, budget_s=12.0)
        s = t.get(1)
        assert s.last_fingerprint == "abc"
        # Worker reported 3 on first dispatch: max(0+1, 3) = 3.
        assert s.attempt == 3
        assert s.budget_s == 12.0

    def test_update_from_worker_keeps_fingerprint_and_budget_when_empty(self):
        # Empty/zero fingerprint+budget must NOT clobber existing tracked
        # values. Attempt, however, always grows (see counter test below).
        t = ResumeTracker()
        t.update_from_worker(1, fingerprint="abc", attempt=2, budget_s=5.0)
        t.update_from_worker(1, fingerprint="", attempt=0, budget_s=0.0)
        s = t.get(1)
        assert s.last_fingerprint == "abc"
        assert s.budget_s == 5.0

    def test_update_from_worker_attempt_grows_per_dispatch(self):
        # Each call == one dispatch; the pipeline-side counter must grow by at
        # least 1 even when the worker keeps reporting tentativa=0 or 1 (which
        # is what happens when the PVC progress file is missing or the worker
        # resets the workspace). Without this, the ceiling in stages.py never
        # bites — regression on #283 (50+ "incompleto sem PR" parks).
        t = ResumeTracker()
        for _ in range(5):
            t.update_from_worker(1, fingerprint="x", attempt=1, budget_s=0.0)
        assert t.get(1).attempt == 5

    def test_update_from_worker_attempt_honors_worker_when_higher(self):
        # When the worker DOES have an authoritative larger count (durable PVC
        # bookkeeping survives), that value wins.
        t = ResumeTracker()
        t.update_from_worker(1, fingerprint="x", attempt=10, budget_s=0.0)
        assert t.get(1).attempt == 10
        t.update_from_worker(1, fingerprint="x", attempt=11, budget_s=0.0)
        assert t.get(1).attempt == 11

    def test_update_from_worker_budget_only_grows(self):
        # Budget is cumulative wall-clock; later report must never shrink it.
        t = ResumeTracker()
        t.update_from_worker(1, fingerprint="x", attempt=1, budget_s=30.0)
        t.update_from_worker(1, fingerprint="x", attempt=2, budget_s=10.0)
        assert t.get(1).budget_s == 30.0

    def test_clear_drops_state(self):
        t = ResumeTracker()
        t.get(1).attempt = 5
        t.clear(1)
        assert t.peek(1) is None

    def test_cadence_immediate_when_interval_zero(self):
        t = ResumeTracker()
        t.record_dispatch(1, 100.0)
        assert t.cadence_ok(1, 100.5, 0) is True

    def test_cadence_blocks_within_interval(self):
        t = ResumeTracker()
        t.record_dispatch(1, 100.0)
        assert t.cadence_ok(1, 105.0, 60) is False
        assert t.cadence_ok(1, 161.0, 60) is True

    def test_cadence_first_attempt_always_ok(self):
        t = ResumeTracker()
        assert t.cadence_ok(99, 0.0, 60) is True

    def test_zero_progress_requires_match(self):
        t = ResumeTracker()
        t.update_from_worker(1, fingerprint="abc", attempt=1, budget_s=0.0)
        assert t.is_zero_progress(1, "abc") is True
        assert t.is_zero_progress(1, "def") is False

    def test_zero_progress_false_on_first_attempt(self):
        t = ResumeTracker()
        assert t.is_zero_progress(1, "abc") is False

    def test_zero_progress_false_when_new_fingerprint_empty(self):
        # A missing measurement must err toward continuing, never block.
        t = ResumeTracker()
        t.update_from_worker(1, fingerprint="abc", attempt=1, budget_s=0.0)
        assert t.is_zero_progress(1, "") is False
