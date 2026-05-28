"""Tests for deile.logging.log_patterns — regex pattern catalog."""

from __future__ import annotations

import pytest

from deile.logging.log_patterns import (
    ALL_PATTERNS,
    AUTH_EXPIRED_PATTERNS,
    CRASH_PATTERNS,
    PIPELINE_PATTERNS,
    RUNTIME_ERROR_PATTERNS,
    Severity,
    match_critical,
    match_line,
)


class TestPatternCatalog:
    """Structural tests for the pattern catalog."""

    def test_catalog_is_non_empty(self):
        assert len(ALL_PATTERNS) > 0

    def test_all_patterns_includes_sublists(self):
        all_names = {p.name for p in ALL_PATTERNS}
        for sublist in (AUTH_EXPIRED_PATTERNS, CRASH_PATTERNS,
                         RUNTIME_ERROR_PATTERNS, PIPELINE_PATTERNS):
            for pat in sublist:
                assert pat.name in all_names, f"{pat.name} missing from ALL_PATTERNS"

    def test_every_pattern_has_name_severity_description(self):
        for pat in ALL_PATTERNS:
            assert isinstance(pat.name, str) and pat.name
            assert pat.severity in (Severity.CRITICAL, Severity.ERROR,
                                    Severity.WARNING, Severity.INFO)
            assert isinstance(pat.description, str) and pat.description

    def test_every_pattern_compiled(self):
        for pat in ALL_PATTERNS:
            assert pat.pattern.pattern  # regex string is non-empty

    def test_patterns_are_frozen(self):
        pat = ALL_PATTERNS[0]
        with pytest.raises(Exception):
            pat.name = "hacked"  # dataclass frozen=True


class TestMatchLine:
    """Tests for match_line and match_critical."""

    def test_empty_line_no_match(self):
        assert match_line("") == []
        assert match_line("   ") == []

    @pytest.mark.parametrize("line,expected_names", [
        ("not logged in — please run /login", ["auth_expired_anthropic"]),
        ("invalid authentication credentials", ["auth_expired_anthropic"]),
        ("401 Unauthorized", ["auth_expired_anthropic"]),
        ("please run `claude auth login`", ["auth_expired_anthropic"]),
        ("Incorrect API key provided", ["auth_expired_openai"]),
        ("invalid api key", ["auth_expired_openai"]),
        ("you didn't provide an api key", ["auth_expired_openai"]),
        ("401 … Invalid API key", ["auth_expired_openai"]),
        ("API key not valid", ["auth_expired_google"]),
        ("Permission denied … API key", ["auth_expired_google"]),
        ("403 Access not configured", ["auth_expired_google"]),
        ("WORKER_AUTH_EXPIRED token=abc123", ["worker_auth_expired"]),
        ("worker auth expired at 2026-01-01", ["worker_auth_expired"]),
        ("bad bearer token", ["worker_auth_expired"]),
        ("UNAUTHORIZED: worker request", ["worker_auth_expired"]),
    ])
    def test_auth_patterns(self, line, expected_names):
        matches = match_line(line)
        names = [m.name for m in matches]
        for expected in expected_names:
            assert expected in names, f"'{line}' should match '{expected}'"

    @pytest.mark.parametrize("line,expected_names", [
        ("back-off restarting failed container", ["crash_loop_backoff"]),
        ("container killed OOM", ["crash_loop_backoff"]),
        ("CrashLoopBackOff", ["crash_loop_backoff"]),
        ("received SIGTERM without timely shutdown", ["sigterm_timeout"]),
        ("force killing after grace period", ["sigterm_timeout"]),
        ("segmentation fault", ["segfault"]),
        ("SIGSEGV", ["segfault"]),
        ("signal 11", ["segfault"]),
    ])
    def test_crash_patterns(self, line, expected_names):
        matches = match_line(line)
        names = [m.name for m in matches]
        for expected in expected_names:
            assert expected in names

    @pytest.mark.parametrize("line,expected_names", [
        ("ModuleNotFoundError: No module named 'foo'", ["module_not_found"]),
        ("Connection refused", ["connection_refused"]),
        ("connect refused: localhost:8080", ["connection_refused"]),
        ("ConnectionRefusedError", ["connection_refused"]),
        ("could not connect to host", ["connection_refused"]),
        ("timeout", ["timeout"]),
        ("timed out", ["timeout"]),
        ("TimeoutError", ["timeout"]),
        ("asyncio.TimeoutError", ["timeout"]),
        ("no space left on device", ["disk_full"]),
        ("disk full", ["disk_full"]),
        ("ENOSPC", ["disk_full"]),
        ("out of disk space", ["disk_full"]),
        ("MemoryError", ["memory_error"]),
        ("out of memory", ["memory_error"]),
        ("cannot allocate memory", ["memory_error"]),
        ("killed OOM", ["memory_error"]),
    ])
    def test_runtime_error_patterns(self, line, expected_names):
        matches = match_line(line)
        names = [m.name for m in matches]
        for expected in expected_names:
            assert expected in names

    @pytest.mark.parametrize("line,expected_names", [
        ("ERROR something broke", ["pipeline_error_rate"]),
        ("CRITICAL fatal", ["pipeline_error_rate"]),
        ("Traceback (most recent call last):", ["pipeline_error_rate"]),
        ("Exception: kaboom", ["pipeline_error_rate"]),
        ("tick completed activity=0 issues=[]", ["pipeline_tick_silent"]),
        ("no eligible issues", ["pipeline_tick_silent"]),
        ("dispatch failed", ["dispatch_failed"]),
        ("WORKER_TIMEOUT after 300s", ["dispatch_failed"]),
        ("dispatch_completed ok=False", ["dispatch_failed"]),
        ("implement BLOCKED: no credits", ["dispatch_failed"]),
        ("review BLOCKED: context too large", ["dispatch_failed"]),
    ])
    def test_pipeline_patterns(self, line, expected_names):
        matches = match_line(line)
        names = [m.name for m in matches]
        for expected in expected_names:
            assert expected in names

    def test_multiple_matches_in_one_line(self):
        """Line with ERROR + auth expired should match both."""
        line = "ERROR: not logged in — please run /login"
        matches = match_line(line)
        names = {m.name for m in matches}
        assert "pipeline_error_rate" in names
        assert "auth_expired_anthropic" in names

    def test_normal_log_line_no_match(self):
        """Normal INFO log should match nothing (unless generic ERROR pattern)."""
        line = "2026-05-28 INFO deile.pipeline tick completed"
        matches = match_line(line)
        # pipeline_tick_silent only matches "tick completed ... activity=0"
        assert all(m.name != "pipeline_tick_silent" for m in matches)


class TestMatchCritical:
    """Tests for match_critical."""

    def test_only_returns_critical(self):
        line = "ERROR: not logged in — please run /login"
        matches = match_critical(line)
        for m in matches:
            assert m.severity == Severity.CRITICAL

    def test_critical_matches_includes_auth(self):
        line = "invalid authentication credentials"
        matches = match_critical(line)
        names = [m.name for m in matches]
        assert "auth_expired_anthropic" in names

    def test_non_critical_error_not_returned(self):
        line = "ModuleNotFoundError: No module named 'foo'"
        matches = match_critical(line)
        names = [m.name for m in matches]
        assert "module_not_found" not in names  # ERROR, not CRITICAL
