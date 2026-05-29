"""Cross-validation tests for k8s manifest wiring changes (issue #355).

Parses the actual YAML files to confirm structural properties that
were broken and fixed by this PR:
  - manifest 44: key renamed AUTH_TOKEN → PIPELINE_STATUS_BEARER_TOKEN
  - manifest 35: pipeline-status-bearer volume mounted at /run/secrets/pipeline-status
  - manifest 46: initContainer bootstrap-repo no longer has hardcoded value: "github"
  - manifest 47: forge.kind key present in deile-runtime-config
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_MANIFESTS = Path(__file__).resolve().parents[3] / "infra" / "k8s" / "manifests"


def _load(filename: str) -> dict:
    return yaml.safe_load((_MANIFESTS / filename).read_text())


def _load_first(filename: str) -> dict:
    docs = list(yaml.safe_load_all((_MANIFESTS / filename).read_text()))
    return docs[0]


class TestManifest44PipelineStatusBearerSecret:
    def test_only_pipeline_status_bearer_token_key(self):
        doc = _load("44-pipeline-status-bearer-secret.yaml")
        string_data = doc.get("stringData", {})
        assert "PIPELINE_STATUS_BEARER_TOKEN" in string_data, (
            "Secret must have PIPELINE_STATUS_BEARER_TOKEN key — "
            "pipeline_status_server._read_auth_token() reads that path"
        )
        assert "AUTH_TOKEN" not in string_data, (
            "AUTH_TOKEN would be a silent mismatch: the file on disk is "
            "PIPELINE_STATUS_BEARER_TOKEN, so AUTH_TOKEN is never seen by the server"
        )


class TestManifest35DeileInteractive:
    def _spec(self) -> dict:
        doc = _load("35-deile-interactive.yaml")
        return doc["spec"]["template"]["spec"]

    def test_pipeline_status_bearer_volume_declared(self):
        volumes = self._spec().get("volumes", [])
        names = [v["name"] for v in volumes]
        assert "pipeline-status-bearer" in names, (
            "Volume pipeline-status-bearer must be declared so the Secret "
            "can be mounted into the deile-shell pod"
        )

    def test_pipeline_status_bearer_volume_is_optional(self):
        volumes = self._spec().get("volumes", [])
        vol = next(v for v in volumes if v["name"] == "pipeline-status-bearer")
        assert vol.get("secret", {}).get("optional") is True, (
            "optional: true must be set so the shell pod starts even when "
            "the operator hasn't created the Secret yet"
        )

    def test_pipeline_status_bearer_mounted_at_correct_path(self):
        containers = self._spec().get("containers", [])
        assert containers, "Expected at least one container"
        mounts = containers[0].get("volumeMounts", [])
        ps_mounts = [m for m in mounts if m["name"] == "pipeline-status-bearer"]
        assert ps_mounts, "pipeline-status-bearer volumeMount not found in container"
        assert ps_mounts[0]["mountPath"] == "/run/secrets/pipeline-status", (
            "Must mount at /run/secrets/pipeline-status so the YAML file key "
            "PIPELINE_STATUS_BEARER_TOKEN lands at "
            "/run/secrets/pipeline-status/PIPELINE_STATUS_BEARER_TOKEN"
        )

    def test_auth_token_file_env_var_points_to_correct_path(self):
        containers = self._spec().get("containers", [])
        env = {e["name"]: e.get("value") for e in containers[0].get("env", [])}
        assert "DEILE_PIPELINE_STATUS_AUTH_TOKEN_FILE" in env, (
            "DEILE_PIPELINE_STATUS_AUTH_TOKEN_FILE env var must be set "
            "so the panel client reads the token from the mounted file"
        )
        assert env["DEILE_PIPELINE_STATUS_AUTH_TOKEN_FILE"] == (
            "/run/secrets/pipeline-status/PIPELINE_STATUS_BEARER_TOKEN"
        )


class TestManifest46DeilePipelineDeployment:
    def _init_env(self) -> dict[str, dict]:
        doc = _load_first("46-deile-pipeline-deployment.yaml")
        spec = doc["spec"]["template"]["spec"]
        init_containers = spec.get("initContainers", [])
        bootstrap = next(
            (c for c in init_containers if c["name"] == "bootstrap-repo"),
            None,
        )
        assert bootstrap is not None, "initContainer bootstrap-repo not found"
        return {e["name"]: e for e in bootstrap.get("env", [])}

    def test_forge_kind_not_hardcoded(self):
        env = self._init_env()
        assert "DEILE_FORGE_KIND" in env, "DEILE_FORGE_KIND must be declared in initContainer env"
        entry = env["DEILE_FORGE_KIND"]
        assert "value" not in entry or entry.get("value") != "github", (
            "DEILE_FORGE_KIND must not be hardcoded to 'github'; "
            "operadores GitLab must be able to override via ConfigMap"
        )

    def test_forge_kind_reads_from_configmap(self):
        env = self._init_env()
        entry = env["DEILE_FORGE_KIND"]
        ref = entry.get("valueFrom", {}).get("configMapKeyRef", {})
        assert ref.get("name") == "deile-runtime-config", (
            "DEILE_FORGE_KIND must read from ConfigMap deile-runtime-config"
        )
        assert ref.get("key") == "forge.kind", (
            "ConfigMap key must be forge.kind"
        )
        assert ref.get("optional") is True, (
            "optional: true required — if key absent, shell fallback "
            "${DEILE_FORGE_KIND:-github} in the initContainer script kicks in"
        )


class TestManifest47DeileRuntimeConfig:
    def test_forge_kind_key_present(self):
        doc = _load("47-deile-runtime-config.yaml")
        data = doc.get("data", {})
        assert "forge.kind" in data, (
            "forge.kind must be in deile-runtime-config so the initContainer "
            "configMapKeyRef in manifest 46 can resolve it"
        )

    def test_forge_kind_default_is_github(self):
        doc = _load("47-deile-runtime-config.yaml")
        assert doc["data"]["forge.kind"] == "github", (
            "Default forge.kind should be github for backwards compatibility"
        )
