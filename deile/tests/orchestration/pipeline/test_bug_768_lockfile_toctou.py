"""xfail test for bug #768: lockfile.acquire() TOCTOU race condition.

Bug: acquire() checks path.exists() then calls path.write_text() without any
atomic OS primitive between them. Two concurrent callers both observe
exists()=False before either writes, both acquire the lock, and both believe
they are the exclusive holder.

Fix: Use os.open(O_CREAT|O_EXCL) for atomic creation.
Tracker: #768
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from deile.orchestration.pipeline import lockfile


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 lockfile-toctou — fix pending tracker #768",
)
def test_concurrent_acquire_only_one_succeeds(tmp_path) -> None:
    """Exactly one of two concurrent acquire() calls must succeed.

    When the bug is present:
      - Both threads return 'acquired' (neither raises LockHeldError)

    When fixed:
      - Exactly one thread acquires; the other raises LockHeldError
    """
    lock_path = tmp_path / "pipeline.lock"
    acquired: list[int] = []
    blocked: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def try_acquire(pid: int) -> None:
        try:
            # Synchronize both threads to enter acquire() simultaneously
            barrier.wait(timeout=5.0)
            lockfile.acquire(lock_path, holder_pid=pid)
            acquired.append(pid)
        except lockfile.LockHeldError:
            blocked.append(pid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=try_acquire, args=(1001 + i,))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors, f"Unexpected errors: {errors}"
    assert len(acquired) == 1, (
        f"Expected exactly 1 acquired, got {len(acquired)}: {acquired}. "
        "Both threads acquired the lock simultaneously — TOCTOU confirmed."
    )
    assert len(blocked) == 1, (
        f"Expected exactly 1 blocked, got {len(blocked)}."
    )
