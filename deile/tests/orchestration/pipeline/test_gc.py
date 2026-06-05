"""Tests for deile.orchestration.pipeline.gc — terminal GC (issue #587)."""
from __future__ import annotations

import asyncio
import pytest

from deile.orchestration.forge.refs import IssueRef, PrRef
from deile.orchestration.pipeline.gc import GCOnOpenItemError, run_terminal_gc
from deile.orchestration.pipeline.labels import (
    BATCH_LABEL_PREFIX,
    FOLLOW_UPS_PROCESSED,
    MENTION_DONE,
    REFINAR,
    WORKFLOW_BLOCKED,
    WORKFLOW_CONCLUDED,
    WORKFLOW_DECOMPOSED,
    WORKFLOW_IMPLEMENTING,
    WORKFLOW_NEW,
    WORKFLOW_PR,
    WORKFLOW_REVIEWING,
    make_attempt_label,
    make_batch_label,
    make_refine_attempt_label,
)


# ---------------------------------------------------------------------------
# Fake forge
# ---------------------------------------------------------------------------


class _FakeForge:
    """Minimal forge double for GC tests."""

    def __init__(self, labels):
        self._labels = list(labels)
        self.removed: list = []
        self.added: list = []
        self.remove_raises: Exception | None = None
        self.add_raises: Exception | None = None

    async def get_issue(self, number: int) -> IssueRef:
        return IssueRef(
            number=number, title="t", url="u", labels=tuple(self._labels)
        )

    async def get_pr(self, number: int) -> PrRef:
        return PrRef(
            number=number, title="t", url="u", labels=tuple(self._labels)
        )

    async def remove_labels(self, kind: str, number: int, labels) -> None:
        if self.remove_raises:
            raise self.remove_raises
        self.removed.extend(labels)
        for lb in labels:
            if lb in self._labels:
                self._labels.remove(lb)

    async def add_labels(self, kind: str, number: int, labels) -> None:
        if self.add_raises:
            raise self.add_raises
        self.added.extend(labels)
        self._labels.extend(labels)


# ---------------------------------------------------------------------------
# GCOnOpenItemError
# ---------------------------------------------------------------------------


class TestGCOnOpenItemError:
    def test_raises_for_open_issue(self):
        forge = _FakeForge([WORKFLOW_NEW])
        with pytest.raises(GCOnOpenItemError):
            asyncio.run(
                run_terminal_gc(forge, "issue", 1, "open")
            )

    def test_raises_for_open_pr(self):
        forge = _FakeForge(["~review:pendente"])
        with pytest.raises(GCOnOpenItemError):
            asyncio.run(
                run_terminal_gc(forge, "pr", 1, "open")
            )

    def test_no_api_calls_before_raise(self):
        calls = []

        class _TrackingForge(_FakeForge):
            async def get_issue(self, number):
                calls.append("get_issue")
                return await super().get_issue(number)

        forge = _TrackingForge([WORKFLOW_NEW])
        with pytest.raises(GCOnOpenItemError):
            asyncio.run(run_terminal_gc(forge, "issue", 1, "open"))

        assert calls == [], "no API calls should occur before GCOnOpenItemError"


# ---------------------------------------------------------------------------
# Issue GC — basic
# ---------------------------------------------------------------------------


class TestIssueGC:
    def _run(self, labels, state="closed"):
        forge = _FakeForge(labels)
        result = asyncio.run(
            run_terminal_gc(forge, "issue", 42, state)
        )
        return forge, result

    def test_strips_workflow_new_and_adds_concluded(self):
        forge, result = self._run([WORKFLOW_NEW, "bug"])
        assert result == "success"
        assert WORKFLOW_NEW not in forge._labels
        assert WORKFLOW_CONCLUDED in forge._labels

    def test_strips_all_transitional_workflow_labels(self):
        transitional = [
            WORKFLOW_NEW,
            WORKFLOW_REVIEWING,
            WORKFLOW_IMPLEMENTING,
            WORKFLOW_PR,
            WORKFLOW_BLOCKED,
            "~workflow:em_refinamento",
            "~workflow:em_arquitetura",
            "~workflow:revisada",
            "~workflow:aguardando_stakeholder",
        ]
        forge, result = self._run(transitional)
        assert result == "success"
        for lb in transitional:
            assert lb not in forge._labels, f"expected {lb!r} to be stripped"
        assert WORKFLOW_CONCLUDED in forge._labels

    def test_preserves_workflow_decomposed(self):
        forge, result = self._run([WORKFLOW_DECOMPOSED, WORKFLOW_IMPLEMENTING])
        assert result == "success"
        assert WORKFLOW_DECOMPOSED in forge._labels

    def test_preserves_workflow_concluded(self):
        """Second invocation on already-GC'd issue returns noop."""
        forge, result = self._run([WORKFLOW_CONCLUDED])
        assert result == "noop"
        assert forge.removed == []
        assert forge.added == []

    def test_strips_by_label(self):
        forge, result = self._run([WORKFLOW_NEW, "~by:default"])
        assert result == "success"
        assert "~by:default" not in forge._labels

    def test_strips_batch_label(self):
        batch = make_batch_label("abc12345")
        forge, result = self._run([WORKFLOW_NEW, batch])
        assert result == "success"
        assert batch not in forge._labels

    def test_strips_attempt_labels(self):
        attempt = make_attempt_label(2)
        forge, result = self._run([WORKFLOW_NEW, attempt])
        assert result == "success"
        assert attempt not in forge._labels

    def test_strips_refine_attempt_labels(self):
        refine = make_refine_attempt_label(3)
        forge, result = self._run([WORKFLOW_NEW, refine])
        assert result == "success"
        assert refine not in forge._labels

    def test_strips_mention_done(self):
        forge, result = self._run([WORKFLOW_NEW, MENTION_DONE])
        assert result == "success"
        assert MENTION_DONE not in forge._labels

    def test_strips_refinar(self):
        forge, result = self._run([WORKFLOW_IMPLEMENTING, REFINAR])
        assert result == "success"
        assert REFINAR not in forge._labels

    def test_preserves_priority_labels(self):
        forge, result = self._run([WORKFLOW_NEW, "~prioridade:1"])
        assert result == "success"
        assert "~prioridade:1" in forge._labels

    def test_preserves_project_type_labels(self):
        for label in ("bug", "feature", "intent", "refactor", "enhancement",
                      "infra", "observability"):
            forge, result = self._run([WORKFLOW_NEW, label])
            assert result == "success", f"expected success stripping {label!r}"
            assert label in forge._labels, f"expected {label!r} to be preserved"

    def test_applies_concluded_even_when_no_strip_needed(self):
        """Issue with no transitional labels still gets ~workflow:concluida."""
        forge, result = self._run(["bug", "~prioridade:2"])
        assert result == "success"
        assert WORKFLOW_CONCLUDED in forge._labels

    def test_noop_when_only_concluded_and_project_labels(self):
        forge, result = self._run([WORKFLOW_CONCLUDED, "bug", "~prioridade:0"])
        assert result == "noop"

    def test_multiple_by_labels_all_stripped(self):
        forge, result = self._run([WORKFLOW_NEW, "~by:a", "~by:b"])
        assert result == "success"
        assert "~by:a" not in forge._labels
        assert "~by:b" not in forge._labels


# ---------------------------------------------------------------------------
# PR GC — basic
# ---------------------------------------------------------------------------


class TestPRGC:
    def _run(self, labels, state="merged"):
        forge = _FakeForge(labels)
        result = asyncio.run(
            run_terminal_gc(forge, "pr", 99, state)
        )
        return forge, result

    def test_strips_review_labels(self):
        forge, result = self._run(["~review:pendente"])
        assert result == "success"
        assert "~review:pendente" not in forge._labels

    def test_strips_all_review_variants(self):
        for lb in ("~review:pendente", "~review:em_andamento", "~review:concluida"):
            forge, result = self._run([lb])
            assert result == "success"
            assert lb not in forge._labels

    def test_strips_by_label(self):
        forge, result = self._run(["~review:pendente", "~by:default"])
        assert result == "success"
        assert "~by:default" not in forge._labels

    def test_strips_batch_label(self):
        batch = make_batch_label("deadbeef")
        forge, result = self._run(["~review:em_andamento", batch])
        assert result == "success"
        assert batch not in forge._labels

    def test_strips_attempt_labels(self):
        attempt = make_attempt_label(1)
        forge, result = self._run(["~review:pendente", attempt])
        assert result == "success"
        assert attempt not in forge._labels

    def test_strips_residual_workflow_labels(self):
        forge, result = self._run(["~review:concluida", WORKFLOW_PR, WORKFLOW_IMPLEMENTING])
        assert result == "success"
        assert WORKFLOW_PR not in forge._labels
        assert WORKFLOW_IMPLEMENTING not in forge._labels

    def test_strips_follow_ups_processed(self):
        forge, result = self._run(["~review:concluida", FOLLOW_UPS_PROCESSED])
        assert result == "success"
        assert FOLLOW_UPS_PROCESSED not in forge._labels

    def test_does_not_add_concluded_to_pr(self):
        """PRs never get ~workflow:concluida applied."""
        forge, result = self._run(["~review:pendente"])
        assert result == "success"
        assert WORKFLOW_CONCLUDED not in forge._labels
        assert forge.added == []

    def test_preserves_priority_labels(self):
        forge, result = self._run(["~review:pendente", "~prioridade:3"])
        assert result == "success"
        assert "~prioridade:3" in forge._labels

    def test_noop_when_already_clean(self):
        forge, result = self._run(["bug", "~prioridade:1"])
        assert result == "noop"
        assert forge.removed == []
        assert forge.added == []

    def test_closed_pr_same_as_merged(self):
        forge, result = self._run(["~review:pendente"], state="closed")
        assert result == "success"
        assert "~review:pendente" not in forge._labels


# ---------------------------------------------------------------------------
# Partial failures
# ---------------------------------------------------------------------------


class TestPartialFailures:
    def test_partial_when_remove_labels_raises(self):
        forge = _FakeForge([WORKFLOW_NEW])
        forge.remove_raises = RuntimeError("network error")
        result = asyncio.run(
            run_terminal_gc(forge, "issue", 1, "closed")
        )
        assert result == "partial"

    def test_partial_when_add_labels_raises(self):
        forge = _FakeForge(["bug"])
        forge.add_raises = RuntimeError("network error")
        result = asyncio.run(
            run_terminal_gc(forge, "issue", 1, "closed")
        )
        assert result == "partial"

    def test_partial_remove_still_attempts_add(self):
        """Even when remove fails, we still attempt the add."""
        forge = _FakeForge([WORKFLOW_NEW])
        forge.remove_raises = RuntimeError("boom")
        result = asyncio.run(
            run_terminal_gc(forge, "issue", 1, "closed")
        )
        assert result == "partial"
        # add was attempted despite remove failure
        assert WORKFLOW_CONCLUDED in forge.added

    def test_partial_when_both_fail(self):
        forge = _FakeForge([WORKFLOW_NEW])
        forge.remove_raises = RuntimeError("remove boom")
        forge.add_raises = RuntimeError("add boom")
        result = asyncio.run(
            run_terminal_gc(forge, "issue", 1, "closed")
        )
        assert result == "partial"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_issue_gc_idempotent_sequential(self):
        """Running GC twice on a closed issue: first success, then noop."""
        forge = _FakeForge([WORKFLOW_NEW, "bug"])
        r1 = asyncio.run(run_terminal_gc(forge, "issue", 1, "closed"))
        assert r1 == "success"
        r2 = asyncio.run(run_terminal_gc(forge, "issue", 1, "closed"))
        assert r2 == "noop"

    def test_pr_gc_idempotent_sequential(self):
        """Running GC twice on a merged PR: first success, then noop."""
        forge = _FakeForge(["~review:pendente", "~prioridade:1"])
        r1 = asyncio.run(run_terminal_gc(forge, "pr", 1, "merged"))
        assert r1 == "success"
        r2 = asyncio.run(run_terminal_gc(forge, "pr", 1, "merged"))
        assert r2 == "noop"


# ---------------------------------------------------------------------------
# Timeout parameter accepted (smoke)
# ---------------------------------------------------------------------------


class TestApiTimeout:
    def test_custom_timeout_accepted(self):
        """api_timeout_s parameter is accepted without error."""
        forge = _FakeForge([WORKFLOW_NEW])
        result = asyncio.run(
            run_terminal_gc(forge, "issue", 1, "closed", api_timeout_s=30.0)
        )
        assert result == "success"

    def test_timeout_triggers_partial(self):
        """Simulates asyncio.TimeoutError from forge call."""
        import asyncio as _asyncio

        class _SlowForge(_FakeForge):
            async def get_issue(self, number):
                raise _asyncio.TimeoutError()

        forge = _SlowForge([WORKFLOW_NEW])
        with pytest.raises(_asyncio.TimeoutError):
            asyncio.run(
                run_terminal_gc(forge, "issue", 1, "closed", api_timeout_s=0.001)
            )
