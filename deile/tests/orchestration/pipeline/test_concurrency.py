"""Tests for multi-monitor concurrency safety and identity-based partitioning.

Validates that parallel monitors with distinct identities:
- partition the issue space without overlap (sharding)
- produce distinct branch names and worktree subdirs
- correctly express ownership via labels
"""

from __future__ import annotations

import pytest

from deile.orchestration.pipeline.identity import (IdentityError,
                                                   MonitorIdentity)


class TestParallelMonitorSafety:

    # ------------------------------------------------------------------
    # Sharding correctness
    # ------------------------------------------------------------------

    def test_sharding_partitions_titles(self):
        """For shard_count=2, every title is owned by exactly one monitor."""
        id_a = MonitorIdentity(monitor_id="a", shard_index=0, shard_count=2)
        id_b = MonitorIdentity(monitor_id="b", shard_index=1, shard_count=2)

        titles = [f"issue title number {i}" for i in range(100)]
        for title in titles:
            owns_a = id_a.owns(title)
            owns_b = id_b.owns(title)
            # Exactly one shard must own each title.
            assert owns_a != owns_b, (
                f"Both or neither shard owns {title!r}: a={owns_a}, b={owns_b}"
            )

    def test_sharding_partitions_across_three_shards(self):
        """For shard_count=3, exactly one of three monitors owns each title."""
        monitors = [
            MonitorIdentity(monitor_id=f"m{i}", shard_index=i, shard_count=3)
            for i in range(3)
        ]
        for i in range(200):
            title = f"title-{i}"
            owners = [m for m in monitors if m.owns(title)]
            assert len(owners) == 1, (
                f"Expected exactly 1 owner for {title!r}, got {len(owners)}"
            )

    def test_default_identity_owns_all(self):
        """With shard_count=1 (default), owns() always returns True."""
        identity = MonitorIdentity()
        titles = ["any title", "", "unicode: 日本語", "a" * 256]
        for title in titles:
            assert identity.owns(title), f"Default identity should own {title!r}"

    def test_ownership_is_deterministic(self):
        """Same (shard_index, shard_count, title) always produces the same result."""
        id_x = MonitorIdentity(monitor_id="x", shard_index=0, shard_count=4)
        title = "deterministic test title"
        results = [id_x.owns(title) for _ in range(100)]
        assert len(set(results)) == 1, "owns() is non-deterministic"

    # ------------------------------------------------------------------
    # Branch / worktree namespacing
    # ------------------------------------------------------------------

    def test_different_monitors_get_different_branches_for_same_issue(self):
        """Two named monitors produce different branch names for issue #42."""
        id_a = MonitorIdentity(monitor_id="a")
        id_b = MonitorIdentity(monitor_id="b")

        # branch_for_issue logic mirrors monitor.py's branch_for_issue()
        def branch_for_issue(identity: MonitorIdentity, issue_number: int) -> str:
            if identity.is_default:
                return f"auto/issue-{issue_number}"
            return f"{identity.branch_prefix('auto')}/issue-{issue_number}"

        branch_a = branch_for_issue(id_a, 42)
        branch_b = branch_for_issue(id_b, 42)

        assert branch_a != branch_b
        assert "a" in branch_a
        assert "b" in branch_b

    def test_default_identity_branch_uses_legacy_prefix(self):
        identity = MonitorIdentity()
        assert identity.branch_prefix("auto") == "auto"

    def test_named_identity_branch_includes_monitor_id(self):
        identity = MonitorIdentity(monitor_id="worker-1")
        assert identity.branch_prefix("auto") == "auto/worker-1"

    def test_different_monitors_get_different_worktree_subdirs(self):
        """Named monitors return distinct non-None worktree subdirs."""
        id_a = MonitorIdentity(monitor_id="monitor-alpha")
        id_b = MonitorIdentity(monitor_id="monitor-beta")

        subdir_a = id_a.worktree_subdir()
        subdir_b = id_b.worktree_subdir()

        assert subdir_a is not None
        assert subdir_b is not None
        assert subdir_a != subdir_b

    def test_default_identity_worktree_subdir_is_none(self):
        """Default identity uses no per-monitor subdirectory (legacy path)."""
        identity = MonitorIdentity()
        assert identity.worktree_subdir() is None

    # ------------------------------------------------------------------
    # Ownership labels
    # ------------------------------------------------------------------

    def test_ownership_label_unique_per_id(self):
        """Different monitor IDs produce different ownership labels."""
        identity1 = MonitorIdentity(monitor_id="alpha")
        identity2 = MonitorIdentity(monitor_id="beta")
        assert identity1.ownership_label() != identity2.ownership_label()

    def test_ownership_label_format(self):
        """Ownership label follows the ~by:<id> convention."""
        identity = MonitorIdentity(monitor_id="my-worker")
        assert identity.ownership_label() == "~by:my-worker"

    def test_default_identity_ownership_label(self):
        """Default identity produces ~by:default."""
        identity = MonitorIdentity()
        assert identity.ownership_label() == "~by:default"

    # ------------------------------------------------------------------
    # Identity validation
    # ------------------------------------------------------------------

    def test_invalid_monitor_id_raises(self):
        with pytest.raises(IdentityError):
            MonitorIdentity(monitor_id="bad/id")

    def test_invalid_shard_count_raises(self):
        with pytest.raises(IdentityError):
            MonitorIdentity(shard_count=0)

    def test_shard_index_out_of_range_raises(self):
        with pytest.raises(IdentityError):
            MonitorIdentity(shard_index=2, shard_count=2)

    def test_valid_boundary_identity(self):
        """shard_index == shard_count-1 is valid."""
        identity = MonitorIdentity(monitor_id="last", shard_index=1, shard_count=2)
        assert identity.shard_index == 1

    def test_is_default_requires_all_defaults(self):
        """is_default is False even when only one parameter differs."""
        assert MonitorIdentity().is_default
        assert not MonitorIdentity(monitor_id="x").is_default
        assert not MonitorIdentity(shard_index=0, shard_count=2).is_default
