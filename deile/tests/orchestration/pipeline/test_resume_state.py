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
        assert s.attempt == 3
        assert s.budget_s == 12.0

    def test_update_from_worker_ignores_empty(self):
        # Empty/zero values must NOT clobber existing tracked state.
        t = ResumeTracker()
        t.update_from_worker(1, fingerprint="abc", attempt=2, budget_s=5.0)
        t.update_from_worker(1, fingerprint="", attempt=0, budget_s=0.0)
        s = t.get(1)
        assert s.last_fingerprint == "abc"
        assert s.attempt == 2
        assert s.budget_s == 5.0

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
