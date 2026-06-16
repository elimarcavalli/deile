"""Tests for deile.log_mgmt.log_dispatcher — worker dispatch for anomalies."""

from __future__ import annotations

from deile.log_mgmt.log_analyzer import Anomaly
from deile.log_mgmt.log_dispatcher import (
    _build_investigation_brief,
    _get_worker_bearer,
    dispatch_anomalies,
    is_auto_dispatch_enabled,
)
from deile.log_mgmt.log_patterns import Severity


class TestBuildInvestigationBrief:
    """Tests for _build_investigation_brief."""

    def test_single_anomaly(self):
        anomalies = [
            {
                "pattern_name": "auth_expired",
                "severity": Severity.CRITICAL,
                "pod_name": "deile-pipeline",
                "count": 3,
                "sample_lines": ["line1", "line2"],
            }
        ]
        brief = _build_investigation_brief(anomalies)
        assert "auth_expired" in brief
        assert "deile-pipeline" in brief
        assert "DEILE-One" in brief

    def test_multiple_anomalies(self):
        anomalies = [
            {
                "pattern_name": "error_rate_spike",
                "severity": "warning",
                "pod_name": "pod1",
                "count": 50,
            },
            {
                "pattern_name": "crash_loop",
                "severity": "critical",
                "pod_name": "pod2",
                "count": 5,
            },
        ]
        brief = _build_investigation_brief(anomalies)
        assert "error_rate_spike" in brief
        assert "crash_loop" in brief

    def test_empty_anomalies(self):
        brief = _build_investigation_brief([])
        assert "DEILE-One" in brief
        # Should still be a valid brief even with no anomalies

    def test_includes_samples(self):
        anomalies = [
            {
                "pattern_name": "test",
                "severity": "info",
                "pod_name": "pod1",
                "count": 1,
                "sample_lines": ["sample-error-1", "sample-error-2", "sample-error-3"],
            }
        ]
        brief = _build_investigation_brief(anomalies)
        assert "sample-error-1" in brief
        assert "sample-error-2" in brief


class TestGetWorkerBearer:
    """Tests for _get_worker_bearer."""

    def test_env_var(self, monkeypatch):
        import os as _os

        monkeypatch.setenv("DEILE_WORKER_BEARER_TOKEN", "test-token-123")
        monkeypatch.setenv("DEILE_WORKER_AUTH_TOKEN_FILE", "")
        # Prevent /run/secrets/worker/AUTH_TOKEN from being found
        real_isfile = _os.path.isfile

        def _mock_isfile(p):
            if p == "/run/secrets/worker/AUTH_TOKEN":
                return False
            return real_isfile(p)

        monkeypatch.setattr("os.path.isfile", _mock_isfile)
        token = _get_worker_bearer()
        assert token == "test-token-123"

    def test_file_token(self, tmp_path, monkeypatch):
        import os as _os

        token_file = tmp_path / "token"
        token_file.write_text("file-token-456")
        monkeypatch.setenv("DEILE_WORKER_AUTH_TOKEN_FILE", str(token_file))
        monkeypatch.delenv("DEILE_WORKER_BEARER_TOKEN", raising=False)
        # Prevent /run/secrets/worker/AUTH_TOKEN from being found
        real_isfile = _os.path.isfile

        def _mock_isfile(p):
            if p == "/run/secrets/worker/AUTH_TOKEN":
                return False
            return real_isfile(p)

        monkeypatch.setattr("os.path.isfile", _mock_isfile)
        token = _get_worker_bearer()
        assert token == "file-token-456"


class TestDispatchAnomalies:
    """Tests for dispatch_anomalies."""

    def test_empty_anomalies_returns_none(self):
        result = dispatch_anomalies([])
        assert result is None

    def test_no_token_returns_none(self, monkeypatch):
        monkeypatch.delenv("DEILE_WORKER_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("DEILE_WORKER_AUTH_TOKEN_FILE", raising=False)
        result = dispatch_anomalies(
            [{"pattern_name": "test", "severity": "info", "pod_name": "p", "count": 1}]
        )
        assert result is None

    def test_converts_anomaly_objects(self, monkeypatch):
        monkeypatch.delenv("DEILE_WORKER_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("DEILE_WORKER_AUTH_TOKEN_FILE", raising=False)
        a = Anomaly(
            pattern_name="test",
            severity=Severity.WARNING,
            pod_name="pod1",
            count=3,
        )
        # No token -> returns None, but doesn't crash
        result = dispatch_anomalies([a])
        assert result is None

    def test_converts_raw_strings(self, monkeypatch):
        monkeypatch.delenv("DEILE_WORKER_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("DEILE_WORKER_AUTH_TOKEN_FILE", "/nonexistent/path/token")
        # Should handle raw strings gracefully without crashing
        result = dispatch_anomalies(["raw anomaly string"])
        assert result is None


class TestAutoDispatchEnabled:
    """Tests for is_auto_dispatch_enabled."""

    def test_false_by_default(self, monkeypatch):
        monkeypatch.delenv("DEILE_LOG_AUTO_DISPATCH", raising=False)
        assert is_auto_dispatch_enabled() is False

    def test_true_when_set(self, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_AUTO_DISPATCH", "true")
        assert is_auto_dispatch_enabled() is True

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_AUTO_DISPATCH", "TRUE")
        assert is_auto_dispatch_enabled() is True

    def test_other_values_false(self, monkeypatch):
        monkeypatch.setenv("DEILE_LOG_AUTO_DISPATCH", "yes")
        assert is_auto_dispatch_enabled() is False
