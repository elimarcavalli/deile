"""Regression tests for AuditLogger initialization.

Covers the fresh-install scenario where ``~/.deile`` does not exist yet —
historically ``log_dir.mkdir(exist_ok=True)`` (no ``parents=True``) raised
``FileNotFoundError`` and prevented the security module from loading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deile.security.audit_logger import AuditLogger


def test_audit_logger_creates_nested_log_dir(tmp_path: Path) -> None:
    """A nested log_dir whose parent doesn't exist must be created."""
    log_dir = tmp_path / "nonexistent_parent" / "logs"
    assert not log_dir.parent.exists()

    logger = AuditLogger(log_dir=str(log_dir))

    assert log_dir.exists()
    assert log_dir.is_dir()
    # The logger's file destination should also be reachable.
    assert logger.log_file.parent == log_dir


def test_audit_logger_default_log_dir_works_on_fresh_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ``~/.deile/logs`` must work even when ``~/.deile`` is missing."""
    fake_home = tmp_path / "fresh_home"
    fake_home.mkdir()
    assert not (fake_home / ".deile").exists()

    monkeypatch.setenv("HOME", str(fake_home))
    # ``Path.home()`` consults ``HOME`` on POSIX.
    logger = AuditLogger()

    expected = fake_home / ".deile" / "logs"
    assert expected.exists()
    assert logger.log_dir == expected
