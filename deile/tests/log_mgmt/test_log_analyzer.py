"""Tests for deile.log_mgmt.log_analyzer — anomaly detection engine."""

from __future__ import annotations

import tempfile
from pathlib import Path

from deile.log_mgmt.log_analyzer import (
    Anomaly,
    _detect_auth_expiry,
    _detect_error_spike,
    _detect_flooding,
    _detect_silent_pipeline,
    _get_config,
    _scan_files,
    scan_crash_loops,
    scan_logs,
)
from deile.log_mgmt.log_patterns import Severity


class TestConfig:
    """Tests for _get_config and env parsing."""

    def test_defaults(self, monkeypatch):
        for key in (
            "DEILE_LOG_ANALYZER_ENABLED",
            "DEILE_LOG_ANALYZER_INTERVAL_S",
            "DEILE_LOG_ERROR_RATE_THRESHOLD",
            "DEILE_LOG_FLOOD_THRESHOLD",
            "DEILE_LOG_PIPELINE_SILENT_TICK_THRESHOLD",
            "DEILE_LOG_AUTO_DISPATCH",
            "DEILE_LOG_DIR",
            "DEILE_NAMESPACE",
        ):
            monkeypatch.delenv(key, raising=False)
        cfg = _get_config()
        assert cfg["enabled"] is True
        assert cfg["interval_s"] == 300
        assert cfg["error_rate_threshold"] == 10
        assert cfg["flood_threshold"] == 200
        assert cfg["silent_tick_threshold"] == 30
        assert cfg["auto_dispatch"] is False
        assert cfg["log_dir"] == "/home/deile/logs"
        assert cfg["namespace"] == "deile"

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_ANALYZER_ENABLED", "false")
        cfg = _get_config()
        assert cfg["enabled"] is False

    def test_custom_values(self, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_ANALYZER_INTERVAL_S", "60")
        monkeypatch.setenv("DEILE_LOG_ERROR_RATE_THRESHOLD", "20")
        monkeypatch.setenv("DEILE_LOG_FLOOD_THRESHOLD", "50")
        monkeypatch.setenv("DEILE_LOG_PIPELINE_SILENT_TICK_THRESHOLD", "10")
        monkeypatch.setenv("DEILE_LOG_AUTO_DISPATCH", "true")
        monkeypatch.setenv("DEILE_LOG_DIR", "/tmp/logs")
        monkeypatch.setenv("DEILE_NAMESPACE", "test-ns")
        cfg = _get_config()
        assert cfg["interval_s"] == 60
        assert cfg["error_rate_threshold"] == 20
        assert cfg["flood_threshold"] == 50
        assert cfg["silent_tick_threshold"] == 10
        assert cfg["auto_dispatch"] is True
        assert cfg["log_dir"] == "/tmp/logs"
        assert cfg["namespace"] == "test-ns"


class TestScanFiles:
    """Tests for _scan_files."""

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = _scan_files(d)
            assert result == {}

    def test_nonexistent_dir(self):
        result = _scan_files("/nonexistent/path/xyz")
        assert result == {}

    def test_scans_pod_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            pod_dir = Path(d) / "deile-pipeline"
            pod_dir.mkdir()
            (pod_dir / "deile-pipeline.log").write_text("line1\nline2\nline3")
            result = _scan_files(d)
            assert "deile-pipeline" in result
            assert result["deile-pipeline"] == ["line1", "line2", "line3"]

    def test_pod_filter(self):
        with tempfile.TemporaryDirectory() as d:
            for pod in ("pod-a", "pod-b", "pod-c"):
                pd = Path(d) / pod
                pd.mkdir()
                (pd / f"{pod}.log").write_text("test")
            result = _scan_files(d, pod_filter=["pod-a", "pod-c"])
            assert set(result.keys()) == {"pod-a", "pod-c"}

    def test_skips_dirs_without_log_file(self):
        with tempfile.TemporaryDirectory() as d:
            pod_dir = Path(d) / "empty-pod"
            pod_dir.mkdir()
            result = _scan_files(d)
            assert "empty-pod" not in result

    def test_skips_non_directory_entries(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "not-a-dir.txt").write_text("nope")
            result = _scan_files(d)
            assert result == {}


class TestDetectErrorSpike:
    """Tests for _detect_error_spike."""

    def test_no_errors(self):
        lines = ["INFO all good", "DEBUG nothing here", "INFO still fine"]
        result = _detect_error_spike("pod1", lines, threshold=10)
        assert result == []

    def test_below_threshold(self):
        lines = ["ERROR something"] * 5  # 5 errors in 5 min = 1/min
        result = _detect_error_spike("pod1", lines, threshold=10, window_minutes=5)
        assert result == []

    def test_above_threshold(self):
        lines = ["ERROR something"] * 60  # 60 errors in 5 min = 12/min
        result = _detect_error_spike("pod1", lines, threshold=10, window_minutes=5)
        assert len(result) == 1
        a = result[0]
        assert a.pattern_name == "error_rate_spike"
        assert a.severity == Severity.WARNING
        assert a.pod_name == "pod1"
        assert a.count == 60
        assert a.threshold == 10

    def test_counts_error_critical_traceback(self):
        lines = ["ERROR x"] * 5 + ["CRITICAL y"] * 3 + ["Traceback z"] * 2
        result = _detect_error_spike("pod1", lines, threshold=0, window_minutes=10)
        assert len(result) == 1
        assert result[0].count == 10

    def test_empty_lines(self):
        result = _detect_error_spike("pod1", [], threshold=1)
        assert result == []


class TestDetectAuthExpiry:
    """Tests for _detect_auth_expiry."""

    def test_no_auth_lines(self):
        lines = ["INFO starting", "DEBUG processing"]
        result = _detect_auth_expiry("pod1", lines)
        assert result == []

    def test_detects_anthropic_expiry(self):
        lines = [
            "not logged in — please run /login",
            "INFO other stuff",
            "invalid authentication credentials",
        ]
        result = _detect_auth_expiry("pod1", lines)
        names = {a.pattern_name for a in result}
        assert "auth_expired_anthropic" in names
        for a in result:
            if a.pattern_name == "auth_expired_anthropic":
                assert a.count == 2
                assert a.severity == Severity.CRITICAL
                assert a.pod_name == "pod1"

    def test_detects_multiple_auth_types(self):
        lines = [
            "invalid authentication credentials",
            "Incorrect API key provided",
            "API key not valid",
            "WORKER_AUTH_EXPIRED token=abc",
        ]
        result = _detect_auth_expiry("pod1", lines)
        names = {a.pattern_name for a in result}
        assert "auth_expired_anthropic" in names
        assert "auth_expired_openai" in names
        assert "auth_expired_google" in names
        assert "worker_auth_expired" in names


class TestDetectFlooding:
    """Tests for _detect_flooding."""

    def test_no_flooding(self):
        lines = ["unique A", "unique B", "unique C"]
        result = _detect_flooding("pod1", lines, threshold=10)
        assert result == []

    def test_below_threshold(self):
        lines = ["2026-05-28T14:00:00 ERROR repeated error"] * 5
        result = _detect_flooding("pod1", lines, threshold=10)
        assert result == []

    def test_flooding_detected(self):
        lines = ["2026-05-28T14:00:00 ERROR flood"] * 250
        result = _detect_flooding("pod1", lines, threshold=200)
        assert len(result) >= 1
        a = result[0]
        assert a.pattern_name == "log_flooding"
        assert a.severity == Severity.WARNING
        assert a.count >= 200

    def test_timestamp_normalization(self):
        """Lines with same body but different timestamps count as same."""
        lines = []
        for i in range(200):
            lines.append(f"2026-05-28T14:{i:02d}:00 ERROR repeated message")
        result = _detect_flooding("pod1", lines, threshold=200)
        assert len(result) >= 1

    def test_limits_to_3_types(self):
        lines = (
            ["2026-05-28T14:00:00 ERROR type1"] * 200
            + ["2026-05-28T14:00:00 ERROR type2"] * 200
            + ["2026-05-28T14:00:00 ERROR type3"] * 200
            + ["2026-05-28T14:00:00 ERROR type4"] * 200
        )
        result = _detect_flooding("pod1", lines, threshold=200)
        assert 1 <= len(result) <= 3

    def test_empty_lines_not_counted(self):
        lines = [""] * 300
        result = _detect_flooding("pod1", lines, threshold=200)
        for a in result:
            assert a.sample_lines != [""]  # empty normalized line ignored


class TestDetectSilentPipeline:
    """Tests for _detect_silent_pipeline."""

    def test_no_silence(self):
        lines = ["INFO tick completed active issues=5"]
        result = _detect_silent_pipeline("pod1", lines, threshold=5)
        assert result == []

    def test_below_threshold(self):
        lines = ["tick completed activity=0"] * 3
        result = _detect_silent_pipeline("pod1", lines, threshold=5)
        assert result == []

    def test_silence_detected(self):
        lines = ["INFO active tick"] * 3 + ["tick completed activity=0"] * 30
        result = _detect_silent_pipeline("pod1", lines, threshold=20)
        assert len(result) == 1
        a = result[0]
        assert a.pattern_name == "pipeline_silent"
        assert a.severity == Severity.INFO
        assert a.count >= 20

    def test_no_eligible_issues_also_silent(self):
        lines = ["no eligible issues"] * 30
        result = _detect_silent_pipeline("pod1", lines, threshold=20)
        assert len(result) == 1

    def test_silence_broken_by_active_tick(self):
        """Active tick in the middle resets the count."""
        lines = (
            ["tick completed activity=0"] * 10
            + ["INFO tick completed active issues=3"]
            + ["tick completed activity=0"] * 10
        )
        result = _detect_silent_pipeline("pod1", lines, threshold=15)
        assert result == []  # max consecutive silent = 10 < 15


class TestScanLogs:
    """Integration tests for scan_logs."""

    def test_no_logs_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            result = scan_logs(log_dir=d)
            assert result == []

    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_ANALYZER_ENABLED", "false")
        result = scan_logs()
        assert result == []

    def test_no_anomalies_in_clean_logs(self):
        with tempfile.TemporaryDirectory() as d:
            pod_dir = Path(d) / "deile-pipeline"
            pod_dir.mkdir()
            (pod_dir / "deile-pipeline.log").write_text(
                "INFO starting\nINFO processing\nINFO done\n"
            )
            result = scan_logs(
                log_dir=d,
                error_rate_threshold=100,
                flood_threshold=1000,
                silent_tick_threshold=100,
            )
            assert result == []

    def test_detects_multiple_anomaly_types(self):
        with tempfile.TemporaryDirectory() as d:
            pod_dir = Path(d) / "deile-pipeline"
            pod_dir.mkdir()
            log_content = (
                "\n".join(["ERROR failure"] * 60)
                + "\n"
                + "not logged in — please run /login\n"
                + "invalid authentication credentials\n"
            )
            (pod_dir / "deile-pipeline.log").write_text(log_content)
            result = scan_logs(
                log_dir=d,
                error_rate_threshold=5,
                flood_threshold=1000,
                silent_tick_threshold=100,
            )
            pattern_names = {a.pattern_name for a in result}
            assert "error_rate_spike" in pattern_names
            assert "auth_expired_anthropic" in pattern_names

    def test_silent_pipeline_only_for_pipeline_pods(self):
        with tempfile.TemporaryDirectory() as d:
            for pod in ("deile-pipeline", "deile-bot"):
                pd = Path(d) / pod
                pd.mkdir()
                (pd / f"{pod}.log").write_text("tick completed activity=0\n" * 30)
            result = scan_logs(log_dir=d, silent_tick_threshold=20)
            # Only pipeline pod should have silent detection
            silent_pods = {
                a.pod_name for a in result if a.pattern_name == "pipeline_silent"
            }
            assert "deile-pipeline" in silent_pods
            assert "deile-bot" not in silent_pods


class TestScanCrashLoops:
    """Tests for scan_crash_loops."""

    def test_no_crash_loops(self):
        counts = {"pod1": 0, "pod2": 1}
        result = scan_crash_loops(counts, threshold=3)
        assert result == []

    def test_crash_loop_detected(self):
        counts = {"pod1": 5, "pod2": 1}
        result = scan_crash_loops(counts, threshold=3)
        assert len(result) == 1
        a = result[0]
        assert a.pattern_name == "crash_loop"
        assert a.severity == Severity.CRITICAL
        assert a.pod_name == "pod1"
        assert a.count == 5

    def test_multiple_crash_loops(self):
        counts = {"pod-a": 4, "pod-b": 3, "pod-c": 1}
        result = scan_crash_loops(counts, threshold=3)
        names = {a.pod_name for a in result}
        assert names == {"pod-a", "pod-b"}

    def test_empty_input(self):
        result = scan_crash_loops({})
        assert result == []


class TestAnomaly:
    """Tests for Anomaly dataclass."""

    def test_to_dict(self):
        a = Anomaly(
            pattern_name="test_pattern",
            severity=Severity.WARNING,
            pod_name="pod1",
            sample_lines=["line1", "line2", "line3", "line4", "line5", "line6"],
            count=42,
            threshold=10,
        )
        d = a.to_dict()
        assert d["pattern_name"] == "test_pattern"
        assert d["severity"] == Severity.WARNING
        assert d["pod_name"] == "pod1"
        assert len(d["sample_lines"]) == 5  # capped at 5
        assert d["count"] == 42
        assert d["threshold"] == 10
