"""Unit tests for MonitorIdentity."""

from __future__ import annotations

import pytest

from deile.orchestration.pipeline.identity import (IdentityError,
                                                   MonitorIdentity)


class TestConstruction:
    def test_default(self):
        i = MonitorIdentity()
        assert i.monitor_id == "default"
        assert i.shard_index == 0
        assert i.shard_count == 1
        assert i.is_default

    def test_custom(self):
        i = MonitorIdentity(monitor_id="m-alfa", shard_index=0, shard_count=2)
        assert not i.is_default
        assert i.shard_count == 2

    def test_invalid_monitor_id_chars(self):
        with pytest.raises(IdentityError):
            MonitorIdentity(monitor_id="bad/id")

    def test_invalid_monitor_id_empty(self):
        with pytest.raises(IdentityError):
            MonitorIdentity(monitor_id="")

    def test_invalid_shard_index_out_of_range(self):
        with pytest.raises(IdentityError):
            MonitorIdentity(monitor_id="m", shard_index=2, shard_count=2)

    def test_invalid_shard_count_zero(self):
        with pytest.raises(IdentityError):
            MonitorIdentity(monitor_id="m", shard_count=0)


class TestFromEnv:
    def test_defaults_when_no_env(self, monkeypatch):
        for k in ("DEILE_PIPELINE_MONITOR_ID", "DEILE_PIPELINE_SHARD_INDEX",
                  "DEILE_PIPELINE_SHARD_COUNT"):
            monkeypatch.delenv(k, raising=False)
        i = MonitorIdentity.from_env()
        assert i.is_default

    def test_reads_env_vars(self, monkeypatch):
        monkeypatch.setenv("DEILE_PIPELINE_MONITOR_ID", "m-beta")
        monkeypatch.setenv("DEILE_PIPELINE_SHARD_INDEX", "1")
        monkeypatch.setenv("DEILE_PIPELINE_SHARD_COUNT", "3")
        i = MonitorIdentity.from_env()
        assert i.monitor_id == "m-beta"
        assert i.shard_index == 1
        assert i.shard_count == 3


class TestSharding:
    def test_owns_everything_when_count_one(self):
        i = MonitorIdentity()
        assert i.owns("any title")
        assert i.owns("another")

    def test_two_shards_partition_keys(self):
        a = MonitorIdentity(monitor_id="a", shard_index=0, shard_count=2)
        b = MonitorIdentity(monitor_id="b", shard_index=1, shard_count=2)
        # For each key, exactly one shard owns it.
        for key in ("issue 1", "issue 2", "feature X", "bug Y"):
            assert a.owns(key) ^ b.owns(key), f"{key!r} ownership not partitioned"

    def test_owns_is_deterministic(self):
        i = MonitorIdentity(monitor_id="m", shard_index=0, shard_count=4)
        assert i.owns("hello") == i.owns("hello")


class TestNamespacing:
    def test_default_branch_prefix_is_legacy(self):
        i = MonitorIdentity()
        assert i.branch_prefix("auto") == "auto"

    def test_custom_branch_prefix_includes_id(self):
        i = MonitorIdentity(monitor_id="m-alfa")
        assert i.branch_prefix("auto") == "auto/m-alfa"

    def test_default_worktree_subdir_is_none(self):
        assert MonitorIdentity().worktree_subdir() is None

    def test_custom_worktree_subdir_is_id(self):
        i = MonitorIdentity(monitor_id="m-alfa")
        assert i.worktree_subdir() == "m-alfa"

    def test_ownership_label_format(self):
        i = MonitorIdentity(monitor_id="m-alfa")
        assert i.ownership_label() == "~by:m-alfa"

    def test_lockfile_name_includes_id(self):
        i = MonitorIdentity(monitor_id="m-alfa")
        assert i.lockfile_name() == ".deile-pipeline-m-alfa.lock"
