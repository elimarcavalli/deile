"""Tests: _build_pod_watch_json schema v1, exact keys, and redaction (issue #461)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel


class TestBuildPodWatchJsonSchemaKeys:
    """AC1: envelope and payload key sets are exact."""

    def test_envelope_exact_keys(self):
        obj = panel._build_pod_watch_json("my-pod", "worker", [], redactor=None)
        assert set(obj.keys()) == {"schema_version", "kind", "exported_at", "payload"}

    def test_payload_exact_keys(self):
        obj = panel._build_pod_watch_json("my-pod", "worker", [], redactor=None)
        assert set(obj["payload"].keys()) == {"pod", "role", "lines"}

    def test_schema_version_v1(self):
        obj = panel._build_pod_watch_json("my-pod", "worker", [], redactor=None)
        assert obj["schema_version"] == "deile.export.v1"

    def test_kind_pod_watch(self):
        obj = panel._build_pod_watch_json("my-pod", "worker", [], redactor=None)
        assert obj["kind"] == "pod_watch"

    def test_pod_name_preserved(self):
        obj = panel._build_pod_watch_json(
            "deile-worker-7d8c", "worker", [], redactor=None
        )
        assert obj["payload"]["pod"] == "deile-worker-7d8c"

    def test_role_preserved(self):
        obj = panel._build_pod_watch_json("my-pod", "claude-worker", [], redactor=None)
        assert obj["payload"]["role"] == "claude-worker"

    def test_lines_empty_list(self):
        obj = panel._build_pod_watch_json("my-pod", "worker", [], redactor=None)
        assert obj["payload"]["lines"] == []

    def test_lines_content_preserved(self):
        lines = ["line one", "line two"]
        obj = panel._build_pod_watch_json("my-pod", "worker", lines, redactor=None)
        assert obj["payload"]["lines"] == lines

    def test_exported_at_present(self):
        obj = panel._build_pod_watch_json("my-pod", "worker", [], redactor=None)
        assert obj["exported_at"]

    def test_json_roundtrip(self):
        """AC6: result is JSON-serializable and round-trips cleanly."""
        lines = ["hello world", "second line"]
        obj = panel._build_pod_watch_json("pod-abc", "pipeline", lines, redactor=None)
        serialized = json.dumps(obj)
        restored = json.loads(serialized)
        assert restored["payload"]["lines"] == lines


class TestBuildPodWatchJsonRedaction:
    """AC2: lines are redacted via SecretsScanner; secret absent, line preserved."""

    def test_redaction_removes_secret_from_lines(self):
        from deile.security.secrets_scanner import SecretsScanner

        redactor = SecretsScanner()
        secret = "ghp_" + "C" * 36
        lines = [f"token={secret}"]
        obj = panel._build_pod_watch_json("pod", "worker", lines, redactor=redactor)
        serialized = json.dumps(obj)
        assert secret not in serialized

    def test_redaction_preserves_line_count(self):
        """AC2 anti-drop: line must be present (redacted), not dropped."""
        from deile.security.secrets_scanner import SecretsScanner

        redactor = SecretsScanner()
        secret = "ghp_" + "C" * 36
        lines = [f"token={secret}"]
        obj = panel._build_pod_watch_json("pod", "worker", lines, redactor=redactor)
        assert len(obj["payload"]["lines"]) == 1

    def test_redaction_line_starts_with_token_prefix(self):
        """Anti-drop: the 'token=' prefix is preserved, only the secret is replaced."""
        from deile.security.secrets_scanner import SecretsScanner

        redactor = SecretsScanner()
        secret = "ghp_" + "C" * 36
        lines = [f"token={secret}"]
        obj = panel._build_pod_watch_json("pod", "worker", lines, redactor=redactor)
        assert obj["payload"]["lines"][0].startswith("token=")

    def test_no_redactor_leaves_lines_unchanged(self):
        lines = ["some log line", "another line"]
        obj = panel._build_pod_watch_json("pod", "worker", lines, redactor=None)
        assert obj["payload"]["lines"] == lines
