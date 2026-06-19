"""xfail test for bug #768: PTY output truncation due to missing post-exit drain.

Bug: _execute_with_pty_unix() checks process.poll() at the top of the read loop
and breaks immediately when the process exits, WITHOUT draining remaining data
buffered in the PTY master fd. Output written by the process before exit but
not yet read by select() is silently discarded.

Fix: After poll() returns non-None, add a drain loop (select with 0.1s timeout)
before breaking.
Tracker: #768
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="PTY path is Unix-only"
)

OUTPUT_SIZE = 5000  # chars; large enough to fill PTY kernel buffer


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 bash-tool-pty-drain — fix pending tracker #768",
)
def test_pty_loop_captures_all_output_when_process_exits_before_select() -> None:
    """All output must be captured even when the process exits before select() fires.

    Reproduces the bug by:
    1. Opening a PTY pair
    2. Running a command that writes OUTPUT_SIZE chars and exits
    3. Sleeping briefly to ensure the process exits before the read loop starts
    4. Running the buggy poll()-first loop pattern

    When the bug is present: captured output is 0 chars (break before any read)
    When fixed: captured output equals OUTPUT_SIZE
    """
    try:
        import pty  # noqa: PLC0415
    except ImportError:
        pytest.skip("pty module not available")

    master_fd, slave_fd = pty.openpty()
    try:
        cmd = f'python3 -c "print(\'X\' * {OUTPUT_SIZE}; import sys; sys.stdout.flush())"; exit 0'
        # Simpler form that avoids quoting issues:
        cmd = sys.executable + f' -c "print(\\\"Y\\\" * {OUTPUT_SIZE})"; exit 0'
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)
        slave_fd = -1  # mark as closed

        # Wait for process to exit before starting the read loop
        # This maximises the chance that poll() fires immediately
        proc.wait(timeout=10.0)

        buf: list[str] = []
        # Replicate the BUGGY loop pattern from bash_tool.py:110-114:
        # poll() check first, break without drain
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # BUG: buffer not drained here
            r, _, _ = select.select([master_fd], [], [], 1.0)
            if r:
                try:
                    chunk = os.read(master_fd, 4096).decode("utf-8", errors="replace")
                    buf.append(chunk)
                except OSError:
                    break

        captured = "".join(buf)
        # Strip trailing newline and terminal escape sequences for comparison
        captured_clean = captured.replace("\r", "").replace("\n", "")

    finally:
        try:
            if slave_fd != -1:
                os.close(slave_fd)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass

    assert len(captured_clean) == OUTPUT_SIZE, (
        f"Expected {OUTPUT_SIZE} chars captured from PTY, got {len(captured_clean)}. "
        "Output was truncated because the read loop broke without draining the PTY buffer."
    )
