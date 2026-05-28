"""Tests for deile.log_mgmt.log_rotator — CappedRotatingFileHandler."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from deile.log_mgmt.log_rotator import (
    CappedRotatingFileHandler,
    _default_log_dir,
    _default_log_file,
    create_log_handler,
    get_pod_name,
)


class TestGetPodName:
    """Tests for get_pod_name heuristics."""

    def test_env_deile_pod_name(self, monkeypatch):
        monkeypatch.setenv("DEILE_POD_NAME", "my-custom-pod")
        monkeypatch.delenv("HOSTNAME", raising=False)
        assert get_pod_name() == "my-custom-pod"

    def test_fallback_to_hostname(self, monkeypatch):
        monkeypatch.delenv("DEILE_POD_NAME", raising=False)
        monkeypatch.setenv("HOSTNAME", "k8s-host-123")
        assert get_pod_name() == "k8s-host-123"

    def test_fallback_to_unknown(self, monkeypatch):
        monkeypatch.delenv("DEILE_POD_NAME", raising=False)
        monkeypatch.delenv("HOSTNAME", raising=False)
        assert get_pod_name() == "unknown"


class TestDefaultPaths:
    """Tests for _default_log_dir and _default_log_file."""

    def test_default_log_dir_with_env(self, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", "/custom/logs")
        assert _default_log_dir("pod1") == "/custom/logs"

    def test_default_log_dir_without_env(self, monkeypatch):
        monkeypatch.delenv("DEILE_LOG_DIR", raising=False)
        monkeypatch.setenv("HOME", "/home/deile")
        result = _default_log_dir("pod1")
        assert result.endswith("/pod1")
        assert result.startswith("/home/deile/logs/")

    def test_default_log_file(self, monkeypatch):
        monkeypatch.delenv("DEILE_LOG_DIR", raising=False)
        monkeypatch.setenv("HOME", "/home/deile")
        result = _default_log_file("pod1")
        assert result.endswith("pod1.log")


class TestCappedRotatingFileHandler:
    """Tests for the CappedRotatingFileHandler."""

    @pytest.fixture
    def tmp_log_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    def test_create_handler_writes_to_file(self, tmp_log_dir, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", tmp_log_dir)
        monkeypatch.setenv("DEILE_POD_NAME", "test-pod")
        handler = create_log_handler("test-pod", max_mb=1, backup_count=2)
        logger = logging.getLogger("test_create")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.addHandler(handler)

        logger.info("hello world")
        handler.flush()

        log_file = Path(tmp_log_dir) / "test-pod.log"
        assert log_file.is_file()
        content = log_file.read_text()
        assert "hello world" in content

    def test_init_logging_via_init_module(self, tmp_log_dir, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", tmp_log_dir)
        monkeypatch.setenv("DEILE_POD_NAME", "test-init")
        from deile.log_mgmt import init_logging

        # Clear root handlers first
        logging.root.handlers.clear()

        handler = init_logging(pod_name="test-init", max_mb=1, backup_count=2)
        logging.info("via init_logging")

        log_file = Path(tmp_log_dir) / "test-init.log"
        assert log_file.is_file()
        content = log_file.read_text()
        assert "via init_logging" in content

    def test_handler_creates_parent_dirs(self, tmp_log_dir):
        path = Path(tmp_log_dir) / "nested" / "deep" / "pod" / "pod.log"
        handler = CappedRotatingFileHandler(str(path), max_mb=1, backup_count=1)
        assert path.parent.is_dir()

    def test_should_rollover_on_size(self, tmp_log_dir):
        path = Path(tmp_log_dir) / "pod.log"
        # tiny max: 1 byte -> rollover on every message
        handler = CappedRotatingFileHandler(str(path), max_mb=1, backup_count=2)
        msg = "x" * 600_000  # ~0.6 MB -> 2 messages should trigger rollover (1.2 MB > 1 MB)
        record = logging.LogRecord(
            "test", logging.INFO, "", 0, msg, (), None)
        handler.format = lambda r: msg  # bypass formatting

        assert handler.should_rollover(record) is False  # first msg fits
        handler.emit(record)
        assert handler.should_rollover(record)  # second msg exceeds 1MB

    def test_day_rotation_triggered(self, tmp_log_dir):
        path = Path(tmp_log_dir) / "pod.log"
        handler = CappedRotatingFileHandler(str(path), max_mb=10, backup_count=2)
        handler.last_rotation_day = 100  # some past day
        current_day = time.gmtime(time.time()).tm_yday

        if current_day == handler.last_rotation_day:
            pytest.skip("Cannot test day rotation when day matches")

        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "msg", (), None)
        handler.format = lambda r: "msg"
        handler.emit(record)
        # After emit, last_rotation_day should be updated
        assert handler.last_rotation_day == current_day

    def test_stdout_handler_dual_write(self, tmp_log_dir, capsys):
        path = Path(tmp_log_dir) / "pod.log"
        handler = CappedRotatingFileHandler(str(path), max_mb=10, backup_count=2)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        handler.set_stdout_handler(stdout_handler)

        logger = logging.getLogger("test_dual")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.addHandler(handler)

        logger.info("dual-write test")

        # File
        assert "dual-write test" in path.read_text()
        # stdout
        captured = capsys.readouterr()
        assert "dual-write test" in captured.out

    def test_create_log_handler_mounts_stdout(self, tmp_log_dir, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", tmp_log_dir)
        handler = create_log_handler("test-stdout", max_mb=5, backup_count=2)
        assert handler._stdout_handler is not None
        assert isinstance(handler._stdout_handler, logging.StreamHandler)

    def test_create_log_handler_env_defaults(self, tmp_log_dir, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_DIR", tmp_log_dir)
        monkeypatch.setenv("DEILE_LOG_MAX_SIZE_MB", "3")
        monkeypatch.setenv("DEILE_LOG_BACKUP_COUNT", "5")
        handler = create_log_handler("test-env-defaults")
        assert handler.maxBytes == 3 * 1024 * 1024
        assert handler.backupCount == 5
