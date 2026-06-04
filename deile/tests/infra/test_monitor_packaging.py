"""Packaging regression for the DEILE-Monitor Phase-A modules.

Each ``infra/k8s/*.py`` baked into the image needs BOTH a Dockerfile ``COPY`` to
``/app/`` AND a matching ``!infra/k8s/<file>`` exception in ``.dockerignore`` —
otherwise the build silently ships without it and the pod crashes on import.
``monitor_tick.py`` also imports its siblings by bare name, so the manifest must
run the flattened ``/app/monitor_tick.py`` (all three co-located in ``/app``).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_MODULES = ("monitor_core.py", "monitor_vigias.py", "monitor_tick.py")


@pytest.fixture(scope="module")
def server_src() -> str:
    return (_REPO / "infra" / "k8s" / "monitor_command_server.py").read_text(encoding="utf-8")


def _monitor_container(manifest_text: str) -> dict:
    for doc in yaml.safe_load_all(manifest_text):
        if (doc and doc.get("kind") == "Deployment"
                and doc.get("metadata", {}).get("name") == "deile-monitor"):
            return doc["spec"]["template"]["spec"]["containers"][0]
    raise AssertionError("deile-monitor Deployment not found in manifest 55")


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return (_REPO / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerignore() -> str:
    return (_REPO / ".dockerignore").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def manifest() -> str:
    return (_REPO / "infra" / "k8s" / "manifests" / "55-deile-monitor-deployment.yaml").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("mod", _MODULES)
def test_dockerfile_copies_module_to_app(dockerfile, mod):
    assert f"COPY --chown=deile:deile infra/k8s/{mod} /app/{mod}" in dockerfile, (
        f"{mod} must be COPY'd to /app in the Dockerfile or the pod crashes on import"
    )


@pytest.mark.parametrize("mod", _MODULES)
def test_dockerignore_excepts_module(dockerignore, mod):
    assert f"!infra/k8s/{mod}" in dockerignore, (
        f"{mod} must have a `!infra/k8s/{mod}` exception in .dockerignore or the COPY fails"
    )


def test_dockerfile_copies_command_server_to_app(dockerfile):
    # monitor_command_server.py is the deile-monitor pod's main process
    # (spec 2026-06-04): it must be baked into /app or the pod cannot start.
    assert (
        "COPY --chown=deile:deile infra/k8s/monitor_command_server.py "
        "/app/monitor_command_server.py" in dockerfile
    ), "monitor_command_server.py must be COPY'd to /app or the pod cannot start"


def test_dockerignore_excepts_command_server(dockerignore):
    assert "!infra/k8s/monitor_command_server.py" in dockerignore, (
        "monitor_command_server.py must have a `!infra/k8s/monitor_command_server.py` "
        "exception in .dockerignore or the COPY fails"
    )


def test_manifest_command_is_the_command_server(manifest):
    """The pod's main process is now the command server (it schedules the tick
    subprocess internally). Asserted against the parsed container `command`, not
    a substring — so a stray mention in a comment can't make this pass."""
    container = _monitor_container(manifest)
    assert container.get("command") == ["python3", "/app/monitor_command_server.py"], (
        "deile-monitor must run monitor_command_server.py as its main process"
    )
    # The legacy bash heartbeat must be gone.
    assert container.get("args") in (None, []), "the bash while-loop args must be removed"


def test_server_runs_flattened_monitor_tick(server_src):
    """The deterministic Phase-A tick still runs as the flattened
    /app/monitor_tick.py subprocess — now spawned by the server, not bash."""
    assert "/app/monitor_tick.py" in server_src, (
        "the server must spawn the flattened /app/monitor_tick.py (Phase A) each tick"
    )


def test_server_runs_phase_b_conditionally(server_src):
    # Phase B only when Phase A wrote the judgment file; spawned by the server.
    assert "monitor-judgment.json" in server_src
    assert "/app/wrapper.py" in server_src and '"monitor"' in server_src


def test_manifest_tick_interval_is_1800(manifest):
    assert 'DEILE_MONITOR_TICK_INTERVAL_S, value: "1800"' in manifest


def test_manifest_caps_tool_iterations(manifest):
    assert 'DEILE_MAX_TOOL_ITERATIONS, value: "50"' in manifest


def test_manifest_grants_secret_rbac_for_renew(manifest):
    assert 'resourceNames: ["claude-credentials"]' in manifest


def test_server_does_not_pass_judgment_json_as_prompt(server_src):
    """Prompt-injection guard (now enforced in the server, not the manifest):
    the untrusted judgment JSON (forge comment text) must NEVER be the CLI
    message to `wrapper.py monitor`. Phase B reads the file itself; the prompt
    arg is the fixed `_PHASE_B_PROMPT` constant."""
    assert "_PHASE_B_PROMPT" in server_src, "Phase B must use the fixed prompt constant"
    # The fixed prompt is a literal string, not derived from the judgment file.
    assert "DADO nao-confiavel" in server_src
    # No shell interpolation of the judgment content into the prompt argv.
    assert "$(cat" not in server_src
