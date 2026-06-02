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

_REPO = Path(__file__).resolve().parents[3]
_MODULES = ("monitor_core.py", "monitor_vigias.py", "monitor_tick.py")


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


def test_manifest_runs_flattened_monitor_tick(manifest):
    assert "python3 /app/monitor_tick.py" in manifest, (
        "manifest 55 must run the flattened /app/monitor_tick.py (Phase A) each tick"
    )


def test_manifest_runs_phase_b_conditionally(manifest):
    # Phase B only when Phase A wrote the judgment file.
    assert "monitor-judgment.json" in manifest
    assert "python3 /app/wrapper.py monitor" in manifest


def test_manifest_tick_interval_is_1800(manifest):
    assert 'DEILE_MONITOR_TICK_INTERVAL_S, value: "1800"' in manifest


def test_manifest_caps_tool_iterations(manifest):
    assert 'DEILE_MAX_TOOL_ITERATIONS, value: "50"' in manifest


def test_manifest_grants_secret_rbac_for_renew(manifest):
    assert 'resourceNames: ["claude-credentials"]' in manifest


def test_manifest_does_not_pass_judgment_json_as_prompt(manifest):
    """Prompt-injection guard: the untrusted judgment JSON (forge comment text)
    must NEVER be the CLI message to `wrapper.py monitor`. Phase B reads the file
    itself via read_file; the prompt arg is a fixed operator instruction."""
    assert "$(cat /state/monitor-judgment.json)" not in manifest, (
        "untrusted judgment JSON must not be interpolated into the Phase B prompt argv"
    )
    assert 'wrapper.py monitor "$(cat' not in manifest
