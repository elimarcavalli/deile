"""Tests for deile.log_mgmt.init_logging — the main entrypoint."""

from __future__ import annotations

import logging
import sys

import pytest

from deile.log_mgmt import init_logging
from deile.log_mgmt.log_rotator import CappedRotatingFileHandler


class TestInitLogging:
    """Tests for init_logging()."""

    @pytest.fixture(autouse=True)
    def _clear_root_handlers(self):
        """Ensure root logger is clean before each test."""
        logging.root.handlers.clear()
        yield
        logging.root.handlers.clear()

    def test_basic_init(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "logs" / "test-pod"
        monkeypatch.setenv("DEILE_LOG_DIR", str(log_dir))
        monkeypatch.setenv("DEILE_POD_NAME", "test-pod")
        # Assert the genuine DEFAULT level — must be hermetic against an
        # ambient DEILE_LOG_LEVEL (the CI workflow sets it to DEBUG globally).
        monkeypatch.delenv("DEILE_LOG_LEVEL", raising=False)

        handler = init_logging(pod_name="test-pod", max_mb=1, backup_count=2)

        assert isinstance(handler, CappedRotatingFileHandler)
        assert logging.root.level == logging.INFO

        # Write a message and check file
        logging.info("test message")
        log_file = log_dir / "test-pod.log"
        assert log_file.is_file()
        content = log_file.read_text()
        assert "test message" in content

    def test_custom_level(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("DEILE_POD_NAME", "test-pod")

        init_logging(pod_name="test-pod", level="DEBUG", max_mb=1, backup_count=1)
        assert logging.root.level == logging.DEBUG

    def test_level_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("DEILE_POD_NAME", "test-pod")
        monkeypatch.setenv("DEILE_LOG_LEVEL", "WARNING")

        init_logging(pod_name="test-pod", max_mb=1, backup_count=1)
        assert logging.root.level == logging.WARNING

    def test_invalid_level_falls_back_to_info(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("DEILE_POD_NAME", "test-pod")
        monkeypatch.setenv("DEILE_LOG_LEVEL", "INVALID_LEVEL")

        init_logging(pod_name="test-pod", max_mb=1, backup_count=1)
        assert logging.root.level == logging.INFO

    def test_pod_name_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("DEILE_POD_NAME", "env-pod")

        init_logging(max_mb=1, backup_count=1)
        logging.info("from env pod")
        log_file = tmp_path / "env-pod.log"
        assert log_file.is_file()

    def test_pod_name_from_hostname(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", str(tmp_path))
        monkeypatch.delenv("DEILE_POD_NAME", raising=False)
        monkeypatch.setenv("HOSTNAME", "hostname-pod")

        init_logging(max_mb=1, backup_count=1)
        logging.info("from hostname")
        log_file = tmp_path / "hostname-pod.log"
        assert log_file.is_file()

    def test_clears_existing_handlers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("DEILE_POD_NAME", "test-pod")

        # Add a dummy handler first (beyond pytest's own LogCaptureHandlers)
        dummy = logging.StreamHandler(sys.stdout)
        logging.root.addHandler(dummy)

        init_logging(pod_name="test-pod", max_mb=1, backup_count=1)
        # Old handlers cleared, only our handler remains (+ pytest LogCaptureHandlers
        # may re-add themselves — so we check ours is there and dummy is gone)
        handlers = logging.root.handlers
        assert dummy not in handlers
        assert any(isinstance(h, CappedRotatingFileHandler) for h in handlers)

    def test_init_logging_return_value_is_handler(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("DEILE_POD_NAME", "test-pod")

        handler = init_logging(pod_name="test-pod", max_mb=1, backup_count=1)
        assert handler is logging.root.handlers[0]
        assert handler._stdout_handler is not None

    def test_env_max_size_and_backup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("DEILE_POD_NAME", "test-pod")
        monkeypatch.setenv("DEILE_LOG_MAX_SIZE_MB", "10")
        monkeypatch.setenv("DEILE_LOG_BACKUP_COUNT", "7")

        handler = init_logging(pod_name="test-pod")
        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 7
