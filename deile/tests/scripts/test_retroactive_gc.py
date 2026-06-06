"""Tests for scripts/retroactive_gc.py (issue #590)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path
from typing import Dict, List

import pytest

from deile.orchestration.forge.refs import IssueRef

# Load scripts/retroactive_gc.py via importlib to avoid sys.path conflicts.
# File is at deile/tests/scripts/test_*.py → parents[3] = repo root.
_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "retroactive_gc.py"
_spec = importlib.util.spec_from_file_location("retroactive_gc", _SCRIPT_PATH)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)  # type: ignore[arg-type]

run_retroactive_gc = _m.run_retroactive_gc
audit_orphan_batch_labels = _m.audit_orphan_batch_labels
_rate_limit_wait = _m._rate_limit_wait
_record_mutation = _m._record_mutation
_MUTATION_TIMESTAMPS = _m._MUTATION_TIMESTAMPS
_should_strip_from_issue = _m._should_strip_from_issue
_should_strip_from_pr = _m._should_strip_from_pr


class _FakeForge:
    def __init__(self, *, issues=None, labels=None):
        self._issues: Dict[int, IssueRef] = {i.number: i for i in (issues or [])}
        self._repo_labels: List[str] = list(labels or [])
        self.removed: Dict[int, List] = {}
        self.added: Dict[int, List] = {}
        self.deleted_labels: List[str] = []

    async def list_issues_with_label(self, label: str, *, limit: int = 50) -> List[IssueRef]:
        return [i for i in self._issues.values() if label in i.labels][:limit]

    async def get_issue(self, number: int):
        return self._issues.get(number)

    async def remove_labels(self, kind: str, number: int, labels) -> None:
        self.removed.setdefault(number, []).extend(labels)

    async def add_labels(self, kind: str, number: int, labels) -> None:
        self.added.setdefault(number, []).extend(labels)
        issue = self._issues.get(number)
        if issue is not None:
            self._issues[number] = IssueRef(
                number=issue.number, title=issue.title, url=issue.url,
                labels=(*issue.labels, *labels), state=issue.state,
            )

    async def list_repo_labels(self) -> List[str]:
        return list(self._repo_labels)

    async def delete_label(self, name: str) -> None:
        if name in self._repo_labels:
            self._repo_labels.remove(name)
        self.deleted_labels.append(name)


def _closed_issue(number: int, *labels: str) -> IssueRef:
    return IssueRef(
        number=number, title="t",
        url=f"https://github.com/o/r/issues/{number}",
        labels=labels, state="closed",
    )


class TestDryRun:
    def test_dry_run_no_mutation(self, tmp_path):
        """--dry-run must not call remove_labels or add_labels."""
        issue = _closed_issue(1, "~workflow:em_pr", "~by:default")
        forge = _FakeForge(issues=[issue])

        result = asyncio.run(
            run_retroactive_gc(forge, dry_run=True, checkpoint_path=tmp_path / "ckpt.json")
        )

        assert result["dry_run"] is True
        assert forge.removed == {}
        assert forge.added == {}


class TestIdempotency:
    def test_idempotency_second_run_noop(self, tmp_path):
        """Second execution skips already-processed items (outcome: noop)."""
        issue = _closed_issue(50, "~workflow:em_pr", "~workflow:nova")
        forge = _FakeForge(issues=[issue])
        ckpt = tmp_path / "ckpt.json"

        asyncio.run(run_retroactive_gc(forge, checkpoint_path=ckpt))
        result2 = asyncio.run(run_retroactive_gc(forge, checkpoint_path=ckpt))

        assert result2["noop"] >= 1


class TestCheckpointResume:
    def test_checkpoint_resume(self, tmp_path):
        """Items already in checkpoint are skipped (resume from item 51 after stop at 50)."""
        ckpt_path = tmp_path / "ckpt.json"
        ckpt_path.write_text(json.dumps({"processed": ["issue:50"], "last_item": "issue:50"}))

        issue_51 = _closed_issue(51, "~workflow:em_pr")
        issue_50 = _closed_issue(50, "~workflow:em_pr")
        forge = _FakeForge(issues=[issue_50, issue_51])

        result = asyncio.run(run_retroactive_gc(forge, checkpoint_path=ckpt_path))

        assert result["noop"] >= 1


class TestAuditOrphanBatchLabels:
    def test_deletes_orphan_batch_labels_preserves_referenced(self):
        """Deletes exactly the orphan batch labels; preserves labels with references."""
        issue_with_ref = IssueRef(
            number=1, title="t", url="u",
            labels=("~batch:aabbccdd", "~batch:11223344"), state="open",
        )

        forge = _FakeForge(
            issues=[issue_with_ref],
            labels=[
                "~batch:aabbccdd",
                "~batch:11223344",
                "~batch:dead0001",
                "~batch:dead0002",
                "~batch:dead0003",
                "~batch:dead0004",
                "~batch:dead0005",
                "bug",
            ],
        )

        result = asyncio.run(audit_orphan_batch_labels(forge))
        assert result["orphans_found"] == 5
        assert result["deleted"] == 5
        assert len(forge.deleted_labels) == 5
        assert "~batch:aabbccdd" not in forge.deleted_labels
        assert "~batch:11223344" not in forge.deleted_labels

    def test_audit_dry_run_no_deletion(self):
        """--dry-run does not delete anything."""
        forge = _FakeForge(labels=["~batch:orphan01", "~batch:orphan02"])

        result = asyncio.run(audit_orphan_batch_labels(forge, dry_run=True))
        assert result["dry_run"] is True
        assert result["orphans_found"] == 2
        assert result["deleted"] == 0
        assert forge.deleted_labels == []


class TestRateLimitCompliance:
    def test_rate_limit_wait_throttles_after_100_per_minute(self):
        """_rate_limit_wait returns > 0 when 100 mutations happened in the last minute."""
        _MUTATION_TIMESTAMPS.clear()
        now = time.monotonic()
        _MUTATION_TIMESTAMPS.extend([now] * 100)

        wait = _rate_limit_wait()
        assert wait > 0
        _MUTATION_TIMESTAMPS.clear()


class TestLabelSetsMatchGcPy:
    def test_workflow_transitional_labels_stripped_from_issues(self):
        """Transitional ~workflow:* labels are stripped from issues (matches gc.py)."""
        for label in ("~workflow:nova", "~workflow:em_revisao", "~workflow:em_implementacao",
                      "~workflow:em_pr", "~workflow:bloqueada"):
            assert _should_strip_from_issue(label), f"expected {label!r} to be stripped"

    def test_workflow_terminal_labels_preserved_from_issues(self):
        """Terminal ~workflow labels are preserved (matches gc.py)."""
        for label in ("~workflow:decomposta", "~workflow:concluida"):
            assert not _should_strip_from_issue(label), f"expected {label!r} to be preserved"

    def test_type_labels_preserved(self):
        """Type labels (bug, feature, etc.) are preserved."""
        for label in ("bug", "feature", "~prioridade:1"):
            assert not _should_strip_from_issue(label)
            assert not _should_strip_from_pr(label)

    def test_review_labels_stripped_from_prs(self):
        """Review labels are stripped from PRs (matches gc.py)."""
        for label in ("~review:pendente", "~review:em_andamento", "~review:concluida"):
            assert _should_strip_from_pr(label), f"expected {label!r} to be stripped"
