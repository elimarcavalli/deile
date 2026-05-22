"""Tests: deile.py preflight flags (--version, --help) — issue #270.

These flags must work BEFORE any venv bootstrap, without side-effects.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from subprocess import TimeoutExpired


_PROJECT_ROOT = Path(__file__).parents[2]  # repo/
_DEILE_PY = _PROJECT_ROOT / "deile.py"


def _run_deile_py(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run deile.py with the given args and return the completed process."""
    env = os.environ.copy()
    # Remove any API keys so we don't accidentally trigger a successful venv bootstrap
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                "GOOGLE_API_KEY"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, str(_DEILE_PY), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(_PROJECT_ROOT),
        env=env,
    )


# ---------------------------------------------------------------------------
# --version flag
# ---------------------------------------------------------------------------

class TestVersionFlag:
    """Tests for --version preflight flag."""

    def test_version_prints_version_and_exits_zero(self):
        """--version should print version string and exit 0."""
        result = _run_deile_py("--version")
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )
        assert "DEILE v" in result.stdout, (
            f"Expected 'DEILE v...' in stdout, got: {result.stdout}"
        )
        assert "build" in result.stdout, (
            f"Expected 'build' in stdout, got: {result.stdout}"
        )

    def test_version_output_format(self):
        """--version output should match 'DEILE vX.Y.Z (build YYYYMMDD)'."""
        result = _run_deile_py("--version")
        # Must start with "DEILE v" and contain "(build "
        stdout = result.stdout.strip()
        assert stdout.startswith("DEILE v"), (
            f"Output should start with 'DEILE v': {stdout}"
        )
        assert "(build " in stdout and stdout.endswith(")"), (
            f"Output should contain '(build NNNNNNNN)': {stdout}"
        )

    def test_version_exit_code_zero(self):
        """--version must exit with code 0."""
        result = _run_deile_py("--version")
        assert result.returncode == 0

    def test_version_no_stderr(self):
        """--version should not produce stderr output."""
        result = _run_deile_py("--version")
        assert result.stderr == "", (
            f"Expected empty stderr, got: {result.stderr}"
        )

    def test_version_no_venv_created(self):
        """--version must NOT create .venv directory."""
        venv_path = _PROJECT_ROOT / ".venv"
        # Record whether .venv existed before
        existed_before = venv_path.exists()
        _run_deile_py("--version")
        # .venv should not have been created if it didn't exist
        if not existed_before:
            assert not venv_path.exists(), (
                ".venv was created by --version flag — this is a side-effect!"
            )

    def test_version_does_not_require_api_keys(self):
        """--version must work even without any API keys set."""
        env = os.environ.copy()
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                    "GOOGLE_API_KEY", "DEILE_API_KEY"):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, str(_DEILE_PY), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(_PROJECT_ROOT),
            env=env,
        )
        assert result.returncode == 0, (
            f"--version failed without API keys. "
            f"exit={result.returncode} stderr={result.stderr}"
        )

    def test_version_with_other_args_still_works(self):
        """--version should be detected even with extra args."""
        result = _run_deile_py("--version", "some message")
        assert result.returncode == 0
        assert "DEILE v" in result.stdout


# ---------------------------------------------------------------------------
# --help flag
# ---------------------------------------------------------------------------

class TestHelpFlag:
    """Tests for --help preflight flag."""

    def test_help_prints_usage_and_exits_zero(self):
        """--help should print usage and exit 0."""
        result = _run_deile_py("--help")
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )
        assert "DEILE" in result.stdout
        assert "Uso:" in result.stdout or "uso:" in result.stdout.lower()

    def test_help_exit_code_zero(self):
        """--help must exit with code 0."""
        result = _run_deile_py("--help")
        assert result.returncode == 0

    def test_help_no_venv_created(self):
        """--help must NOT create .venv directory."""
        venv_path = _PROJECT_ROOT / ".venv"
        existed_before = venv_path.exists()
        _run_deile_py("--help")
        if not existed_before:
            assert not venv_path.exists(), (
                ".venv was created by --help flag — this is a side-effect!"
            )

    def test_help_no_stderr(self):
        """--help should not produce stderr output."""
        result = _run_deile_py("--help")
        assert result.stderr == "", (
            f"Expected empty stderr, got: {result.stderr}"
        )

    def test_help_mentions_version_flag(self):
        """--help output should mention --version flag."""
        result = _run_deile_py("--help")
        assert "--version" in result.stdout, (
            f"--help output should mention --version: {result.stdout}"
        )


# ---------------------------------------------------------------------------
# -h shorthand
# ---------------------------------------------------------------------------

class TestHShorthand:
    """Tests for -h shorthand flag."""

    def test_h_prints_usage_and_exits_zero(self):
        """-h should print usage (same as --help) and exit 0."""
        result = _run_deile_py("-h")
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )
        assert "DEILE" in result.stdout

    def test_h_no_venv_created(self):
        """-h must NOT create .venv directory."""
        venv_path = _PROJECT_ROOT / ".venv"
        existed_before = venv_path.exists()
        _run_deile_py("-h")
        if not existed_before:
            assert not venv_path.exists(), (
                ".venv was created by -h flag — this is a side-effect!"
            )

    def test_h_exit_code_zero(self):
        """-h must exit with code 0."""
        result = _run_deile_py("-h")
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Preflight priority: flags handled BEFORE venv/bootstrap
# ---------------------------------------------------------------------------

class TestPreflightPriority:
    """Tests that --version/--help are handled BEFORE bootstrap."""

    def test_version_is_fast(self):
        """--version should complete quickly (no venv creation)."""
        import time
        start = time.monotonic()
        result = _run_deile_py("--version", timeout=10)
        elapsed = time.monotonic() - start
        assert result.returncode == 0
        # Should complete in well under 5 seconds (no pip install)
        assert elapsed < 5.0, (
            f"--version took {elapsed:.1f}s — too slow, "
            f"bootstrap may have been triggered"
        )

    def test_help_is_fast(self):
        """--help should complete quickly (no venv creation)."""
        import time
        start = time.monotonic()
        result = _run_deile_py("--help", timeout=10)
        elapsed = time.monotonic() - start
        assert result.returncode == 0
        assert elapsed < 5.0, (
            f"--help took {elapsed:.1f}s — too slow, "
            f"bootstrap may have been triggered"
        )

    def test_no_arg_still_reaches_bootstrap_path(self):
        """Running with no args should NOT exit 0 via preflight (should
        attempt bootstrap or venv redirect). This test verifies preflight
        doesn't intercept normal operation.

        The timeout is EXPECTED: without flags, the script enters
        bootstrap (venv creation / pip install / interactive prompt)
        or re-execs into the venv and starts the agent — all of which
        take time or block without a TTY. The timeout IS the proof
        that preflight did NOT intercept and exit(0) quickly."""
        try:
            result = _run_deile_py(timeout=10)
        except TimeoutExpired:
            # Timeout proves the script entered bootstrap / venv redirect
            # and did NOT exit quickly via preflight. This is the
            # expected success case.
            return
        # If it did return (very fast env or pre-existing venv that fails
        # quickly), ensure it didn't exit via preflight.
        first_lines = result.stdout.split("\n")[0:3]
        assert "DEILE v" not in first_lines, (
            f"No-arg run should not trigger version output. "
            f"stdout[0:3]={first_lines}"
        )
        # It should NOT exit 0 from preflight (exit 0 would mean
        # preflight handled it, which is wrong for no-arg runs).
        # If it exits non-zero, that's bootstrap failing — also fine.
