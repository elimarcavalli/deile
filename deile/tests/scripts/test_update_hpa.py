from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "update_hpa.sh"


def _write_runtime_config(path: Path, min_rep: int, max_rep: int, target: str) -> None:
    path.write_text(textwrap.dedent(f"""
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: deile-runtime-config
        data:
          worker.hpa.minReplicas: "{min_rep}"
          worker.hpa.maxReplicas: "{max_rep}"
          worker.hpa.targetAverageValue: "{target}"
    """), encoding="utf-8")


def _stub_kubectl(tmp_path: Path) -> Path:
    script = tmp_path / "kubectl"
    log = tmp_path / "kubectl.log"
    script.write_text(textwrap.dedent("""
        #!/usr/bin/env bash
        log_file="${HPA_PATCH_LOG:-/tmp/hpa.log}"
        printf '%s\n' "$@" >> "$log_file"
        exit 0
    """), encoding="utf-8")
    script.chmod(0o755)
    return script


def _invoke_script(config_path: Path, kubectl_path: Path, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["HPA_PATCH_LOG"] = str(tmp_path / "kubectl.log")
    return subprocess.run(
        [
            "bash",
            str(_SCRIPT_PATH),
            "--config-file",
            str(config_path),
            "--kubectl",
            str(kubectl_path),
        ],
        cwd=_SCRIPT_PATH.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_update_hpa_executes_patch_with_valid_values(tmp_path: Path):
    config = tmp_path / "runtime-config.yaml"
    _write_runtime_config(config, min_rep=2, max_rep=5, target="450m")
    kubectl_stub = _stub_kubectl(tmp_path)

    result = _invoke_script(config, kubectl_stub, tmp_path)
    assert result.returncode == 0, result.stderr.decode()

    log = (tmp_path / "kubectl.log").read_text()
    assert "--patch" in log
    assert "minReplicas: 2" in log
    assert "maxReplicas: 5" in log
    assert "averageValue: \"450m\"" in log


def test_update_hpa_rejects_min_greater_than_max(tmp_path: Path):
    config = tmp_path / "runtime-config.yaml"
    _write_runtime_config(config, min_rep=5, max_rep=3, target="400m")
    kubectl_stub = _stub_kubectl(tmp_path)

    result = _invoke_script(config, kubectl_stub, tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "kubectl.log").exists()


def test_update_hpa_rejects_too_small_max(tmp_path: Path):
    config = tmp_path / "runtime-config.yaml"
    _write_runtime_config(config, min_rep=1, max_rep=1, target="400m")
    kubectl_stub = _stub_kubectl(tmp_path)

    result = _invoke_script(config, kubectl_stub, tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "kubectl.log").exists()
