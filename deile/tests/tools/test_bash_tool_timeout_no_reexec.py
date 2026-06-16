"""Regression test: PTY timeout must NOT re-execute the command via subprocess.

Before the fix, TimeoutError raised inside _execute_with_pty_unix was caught by
the outer `except Exception` handler which then called _execute_with_subprocess
with the same command — causing double execution of any side-effects.

After the fix, an explicit `except TimeoutError: raise` placed before the generic
handler ensures the timeout propagates directly to the caller; the subprocess path
is never entered.
"""

from __future__ import annotations

import os
import sys

import pytest

from deile.tools.bash_tool import BashExecuteTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PTY path is Unix-only")


@pytest.fixture()
def tool() -> BashExecuteTool:
    return BashExecuteTool()


@pytest.fixture(autouse=True)
def _suppress_realtime_output(tool, monkeypatch):
    monkeypatch.setattr(tool, "_should_show_output", lambda: False)


def test_pty_timeout_propagates_and_does_not_call_subprocess(
    tool, tmp_path, monkeypatch
):
    """TimeoutError must propagate; _execute_with_subprocess must not be called."""
    subprocess_call_count = 0

    def spy(*args, **kwargs):
        nonlocal subprocess_call_count
        subprocess_call_count += 1
        # Return sentinel so we can detect if it was reached.
        return ("", "", 1, False)

    monkeypatch.setattr(tool, "_execute_with_subprocess", spy)

    with pytest.raises(TimeoutError):
        tool._execute_with_pty_unix(
            command="sleep 5",
            working_dir=tmp_path,
            env=dict(os.environ),
            timeout=0.1,
        )

    assert subprocess_call_count == 0, (
        f"_execute_with_subprocess was called {subprocess_call_count} time(s) "
        "after a PTY timeout — double execution of side-effects detected"
    )
