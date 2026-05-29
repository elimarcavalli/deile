"""Tests for log PVC wiring fix (issue #404).

Covers:
  - 42b-deile-logs-pvc.yaml uses ReadWriteOnce (compatible with k3s local-path)
  - Both deile-logs and deile-logs-pipeline PVCs are declared in 42b
  - 42b-deile-logs-pvc.yaml is in the manifests list of every DeploymentProfile
  - 42b is applied before Deployments in every profile's manifests tuple
  - 46-deile-pipeline-deployment.yaml mounts deile-logs-pipeline at /home/deile/logs
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_MANIFESTS = _REPO / "infra" / "k8s" / "manifests"

for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import deploy  # noqa: E402


def _load_all(filename: str) -> list[dict]:
    return list(yaml.safe_load_all((_MANIFESTS / filename).read_text()))


class TestLogsPvcYaml:
    """42b-deile-logs-pvc.yaml structure."""

    def test_deile_logs_pvc_uses_rwo(self):
        docs = _load_all("42b-deile-logs-pvc.yaml")
        worker_pvc = next(d for d in docs if d["metadata"]["name"] == "deile-logs")
        modes = worker_pvc["spec"]["accessModes"]
        assert modes == ["ReadWriteOnce"], (
            "deile-logs must use ReadWriteOnce — local-path (k3s default) "
            "does not support ReadWriteMany"
        )

    def test_deile_logs_pipeline_pvc_exists(self):
        docs = _load_all("42b-deile-logs-pvc.yaml")
        names = [d["metadata"]["name"] for d in docs]
        assert "deile-logs-pipeline" in names, (
            "deile-logs-pipeline PVC must be declared in 42b for pipeline "
            "log persistence (issue #404)"
        )

    def test_deile_logs_pipeline_pvc_uses_rwo(self):
        docs = _load_all("42b-deile-logs-pvc.yaml")
        pipeline_pvc = next(d for d in docs if d["metadata"]["name"] == "deile-logs-pipeline")
        modes = pipeline_pvc["spec"]["accessModes"]
        assert modes == ["ReadWriteOnce"], (
            "deile-logs-pipeline must use ReadWriteOnce for k3s local-path compatibility"
        )

    def test_both_pvcs_have_256mi_storage(self):
        docs = _load_all("42b-deile-logs-pvc.yaml")
        for doc in docs:
            storage = doc["spec"]["resources"]["requests"]["storage"]
            assert storage == "256Mi", (
                f"PVC {doc['metadata']['name']} must request 256Mi storage"
            )


class TestDeploymentProfileManifests:
    """42b must appear before the deployments that mount its PVCs."""

    @pytest.mark.parametrize("profile_name", ["pipeline-only", "full", "claude-only"])
    def test_logs_pvc_in_manifests(self, profile_name):
        m = deploy.DeploymentProfile(profile_name).manifests
        assert "42b-deile-logs-pvc.yaml" in m, (
            f"Profile '{profile_name}' manifests must include 42b-deile-logs-pvc.yaml "
            f"so the PVC exists before the Deployments start"
        )

    @pytest.mark.parametrize("profile_name", ["pipeline-only", "full", "claude-only"])
    def test_logs_pvc_before_worker_deployment(self, profile_name):
        m = list(deploy.DeploymentProfile(profile_name).manifests)
        assert "42b-deile-logs-pvc.yaml" in m, f"42b missing from {profile_name}"
        assert "45-deile-worker-deployment.yaml" in m, f"45 missing from {profile_name}"
        idx_pvc = m.index("42b-deile-logs-pvc.yaml")
        idx_dep = m.index("45-deile-worker-deployment.yaml")
        assert idx_pvc < idx_dep, (
            f"Profile '{profile_name}': 42b-deile-logs-pvc.yaml (idx {idx_pvc}) must "
            f"come before 45-deile-worker-deployment.yaml (idx {idx_dep})"
        )

    @pytest.mark.parametrize("profile_name", ["pipeline-only", "full", "claude-only"])
    def test_logs_pvc_before_pipeline_deployment(self, profile_name):
        m = list(deploy.DeploymentProfile(profile_name).manifests)
        assert "42b-deile-logs-pvc.yaml" in m
        assert "46-deile-pipeline-deployment.yaml" in m
        idx_pvc = m.index("42b-deile-logs-pvc.yaml")
        idx_dep = m.index("46-deile-pipeline-deployment.yaml")
        assert idx_pvc < idx_dep, (
            f"Profile '{profile_name}': 42b must come before 46 — pipeline "
            f"deployment now mounts deile-logs-pipeline PVC"
        )


class TestPipelineDeploymentLogsMount:
    """46-deile-pipeline-deployment.yaml must mount deile-logs-pipeline."""

    def _spec(self) -> dict:
        docs = _load_all("46-deile-pipeline-deployment.yaml")
        return docs[0]["spec"]["template"]["spec"]

    def test_logs_volume_declared(self):
        volumes = self._spec().get("volumes", [])
        names = [v["name"] for v in volumes]
        assert "logs" in names, (
            "Volume 'logs' must be declared in deile-pipeline so logs survive rollout"
        )

    def test_logs_volume_uses_deile_logs_pipeline_pvc(self):
        volumes = self._spec().get("volumes", [])
        logs_vol = next((v for v in volumes if v["name"] == "logs"), None)
        assert logs_vol is not None
        claim = logs_vol.get("persistentVolumeClaim", {}).get("claimName")
        assert claim == "deile-logs-pipeline", (
            f"Pipeline logs volume must use deile-logs-pipeline PVC, got: {claim!r}"
        )

    def test_logs_volumemount_at_home_deile_logs(self):
        containers = self._spec().get("containers", [])
        assert containers
        mounts = containers[0].get("volumeMounts", [])
        logs_mounts = [m for m in mounts if m["name"] == "logs"]
        assert logs_mounts, "volumeMount 'logs' not found in pipeline container"
        assert logs_mounts[0]["mountPath"] == "/home/deile/logs", (
            "logs must be mounted at /home/deile/logs — CappedRotatingFileHandler "
            "writes to /home/deile/logs/<pod-name>/ (log_mgmt/log_rotator.py)"
        )


class TestDoCreateNamespaceManifestsOrder:
    """do_create_namespace must apply 42b before deployments."""

    def test_logs_pvc_in_create_namespace_order(self):
        import inspect
        src = inspect.getsource(deploy.do_create_namespace)
        assert "42b-deile-logs-pvc.yaml" in src, (
            "do_create_namespace must include 42b-deile-logs-pvc.yaml in manifests_order"
        )
