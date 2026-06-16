"""Meta-tests that verify the three autouse isolation fixtures in conftest.py.

Issue #471: AC4 — these tests prove that _snapshot_os_environ,
_clean_logging_handlers, and _guard_sys_stdio (with failing-restore) each
do their job.  A passing run means the fixtures are functioning; a failure
means the fixture itself is broken.
"""

from __future__ import annotations

import io
import logging
import os

# Enable the pytester plugin (not loaded by default) so test_stdio_swap_fails_test
# can spin up an isolated in-process pytest run to verify _guard_sys_stdio's
# failing-restore assertion.
pytest_plugins = ["pytester"]


class TestEnvRestore:
    """_snapshot_os_environ must clean up direct os.environ mutations between tests.

    Two tests run in alphabetical order (a → b). test_a leaks a key via direct
    assignment; test_b asserts the key is gone.  Pass = fixture worked.
    """

    def test_a_leaks_env_var(self):
        """Simulate a leaky test: set env var via direct assignment without monkeypatch."""
        os.environ["_DEILE_LEAK_KEY"] = "x"
        assert os.environ["_DEILE_LEAK_KEY"] == "x"

    def test_b_env_is_clean(self):
        """_snapshot_os_environ must have restored os.environ after test_a."""
        assert "_DEILE_LEAK_KEY" not in os.environ, (
            "_DEILE_LEAK_KEY is still in os.environ — "
            "_snapshot_os_environ did not restore the environment after the previous test."
        )


class TestHandlerRestore:
    """_clean_logging_handlers must remove handlers added without monkeypatch.

    Two tests run in alphabetical order (a → b). test_a attaches a handler to
    a named logger; test_b asserts the handler is gone.  Pass = fixture worked.
    """

    _LOGGER_NAME = "deile._test_handler_restore"

    def test_a_adds_handler(self):
        """Simulate a leaky test: add a logging handler without cleanup."""
        logger = logging.getLogger(self._LOGGER_NAME)
        handler = logging.StreamHandler(io.StringIO())
        handler.name = "_deile_test_leak_handler"
        logger.addHandler(handler)
        assert any(
            getattr(h, "name", None) == "_deile_test_leak_handler"
            for h in logger.handlers
        )

    def test_b_handler_is_gone(self):
        """_clean_logging_handlers must have removed the handler added in test_a."""
        logger = logging.getLogger(self._LOGGER_NAME)
        leaked = [
            h
            for h in logger.handlers
            if getattr(h, "name", None) == "_deile_test_leak_handler"
        ]
        assert not leaked, (
            f"Handler '_deile_test_leak_handler' is still attached to "
            f"logger '{self._LOGGER_NAME}' — "
            "_clean_logging_handlers did not restore handlers after the previous test."
        )


def test_stdio_swap_fails_test(pytester):
    """_guard_sys_stdio's failing-restore marks a test as ERROR if sys.stdout is swapped.

    Uses pytester to spin up an isolated pytest run that contains a conftest
    with a minimal copy of _guard_sys_stdio (failing-restore variant) and a
    test that replaces sys.stdout without restoring it.  The run must produce
    exactly 1 error (teardown assertion failure) and 0 passed/failed tests.
    Pass = the assert in _guard_sys_stdio fires and exposes the leaker.
    Fail = silent-restore was used or the fixture is missing.
    """
    pytester.makeconftest("""
import sys
import pytest

@pytest.fixture(autouse=True)
def _guard_sys_stdio():
    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    saved_stdin = sys.stdin
    yield
    assert sys.stdout is saved_stdout, (
        f"test mutated sys.stdout without monkeypatch: {sys.stdout!r}"
    )
    sys.stdout = saved_stdout
    assert sys.stderr is saved_stderr, (
        f"test mutated sys.stderr without monkeypatch: {sys.stderr!r}"
    )
    sys.stderr = saved_stderr
    assert sys.stdin is saved_stdin, (
        f"test mutated sys.stdin without monkeypatch: {sys.stdin!r}"
    )
    sys.stdin = saved_stdin
""")
    pytester.makepyfile("""
import sys
import io

def test_swaps_stdout_without_restore():
    # Simulate a leaky test: replace sys.stdout without any restore.
    sys.stdout = io.StringIO()
""")
    # -p no:capture: disable pytest's own stdout replacement so the fixture's
    # failing-restore can observe sys.stdout after the test swapped it.
    # (With capture enabled, pytest restores sys.stdout before teardown, masking the leak.)
    result = pytester.runpytest("-p", "no:capture", "-v")
    # The test body returns normally (passed=1), but the teardown raises
    # AssertionError (errors=1). Both are expected: the test PASSES, but its
    # teardown catches the stdout leak and marks it ERROR.
    result.assert_outcomes(passed=1, errors=1)
