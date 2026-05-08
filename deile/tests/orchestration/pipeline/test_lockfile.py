"""Unit tests for PID lockfile."""

from __future__ import annotations

import os

import pytest

from deile.orchestration.pipeline.lockfile import (LockHeldError, _pid_alive,
                                                   acquire, pid_lock, release)


class TestPidAlive:
    def test_self_is_alive(self):
        assert _pid_alive(os.getpid())

    def test_huge_pid_not_alive(self):
        assert not _pid_alive(2**31 - 1)

    def test_zero_or_negative_not_alive(self):
        assert not _pid_alive(0)
        assert not _pid_alive(-1)


class TestAcquireRelease:
    def test_acquire_creates_file_with_pid(self, tmp_path):
        path = tmp_path / "x.lock"
        acquire(path)
        assert path.exists()
        assert path.read_text().strip() == str(os.getpid())

    def test_acquire_replaces_stale_pid(self, tmp_path):
        path = tmp_path / "x.lock"
        path.write_text("999999\n")  # almost certainly dead
        acquire(path)
        assert path.read_text().strip() == str(os.getpid())

    def test_acquire_raises_when_live_pid_holds(self, tmp_path):
        path = tmp_path / "x.lock"
        # Write our own current PID, then try to acquire as a different PID.
        path.write_text(f"{os.getpid()}\n")
        with pytest.raises(LockHeldError) as exc:
            acquire(path, holder_pid=os.getpid() + 1)
        assert exc.value.holder_pid == os.getpid()

    def test_release_removes_self_owned_lock(self, tmp_path):
        path = tmp_path / "x.lock"
        acquire(path)
        release(path)
        assert not path.exists()

    def test_release_leaves_foreign_lock(self, tmp_path):
        path = tmp_path / "x.lock"
        path.write_text(f"{os.getpid()}\n")
        release(path, holder_pid=os.getpid() + 1)  # not us
        assert path.exists()

    def test_release_missing_is_noop(self, tmp_path):
        # Should not raise.
        release(tmp_path / "missing.lock")


class TestPidLockContextManager:
    def test_acquires_and_releases(self, tmp_path):
        path = tmp_path / "x.lock"
        with pid_lock(path) as p:
            assert p.exists()
        assert not path.exists()

    def test_releases_on_exception(self, tmp_path):
        path = tmp_path / "x.lock"
        with pytest.raises(RuntimeError):
            with pid_lock(path):
                assert path.exists()
                raise RuntimeError("boom")
        assert not path.exists()
