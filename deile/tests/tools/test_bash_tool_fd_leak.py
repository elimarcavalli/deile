"""Regression test for FD leak in ``BashExecuteTool._execute_with_pty_unix``.

Bug: when a command timed out, the function raised ``TimeoutError``
inside the read loop — that exception escaped the function body without
ever reaching ``os.close(master_fd)`` (the close was a normal statement
AFTER the loop, not in a finally). The outer ``except Exception`` fell
back to the subprocess path, but ``master_fd`` was leaked. Repeated
timeouts exhausted the process's file-descriptor table.

The fix wraps the PTY loop in try/finally that ALWAYS closes master_fd
(and slave_fd if still open) and reaps the subprocess if still alive.
"""

from __future__ import annotations

import os
import resource
import sys

import pytest

from deile.tools.bash_tool import BashExecuteTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PTY path is Unix-only")


def _count_open_fds() -> int:
    pid = os.getpid()
    fd_dir = f"/proc/{pid}/fd"
    if os.path.isdir(fd_dir):
        return len(os.listdir(fd_dir))
    # Fallback: ulimit-based heuristic.
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    return -1  # not available


@pytest.fixture()
def tool() -> BashExecuteTool:
    return BashExecuteTool()


@pytest.fixture(autouse=True)
def _ensure_realtime_off(tool, monkeypatch):
    monkeypatch.setattr(tool, "_should_show_output", lambda: False)


def test_pty_timeout_does_not_leak_master_fd(tool, tmp_path):
    """Trigger several timeouts and ensure the open-FD count stays bounded."""
    before = _count_open_fds()
    if before == -1:
        pytest.skip("/proc/<pid>/fd not available — can't measure FDs")

    # 5 timeouts is enough to detect a per-call leak via the FD delta.
    for _ in range(5):
        try:
            tool._execute_with_pty_unix(
                command="sleep 5",
                working_dir=tmp_path,
                env=dict(os.environ),
                timeout=0.1,
            )
        except TimeoutError:
            pass
        except Exception:
            # Fallback path is OK; we just don't want a leak.
            pass

    after = _count_open_fds()
    # Tolerate a handful of unrelated FDs created by other process activity.
    # Without the fix, ``master_fd`` would leak per iteration → +5.
    assert (
        after - before < 3
    ), f"FD leak detected: before={before} after={after} delta={after - before}"
