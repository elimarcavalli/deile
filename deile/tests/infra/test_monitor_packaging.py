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


def test_manifest_runs_command_server_via_args_preserving_tini(manifest):
    """The pod's main process is the command server, launched via `args` (NOT
    `command`) so the image ENTRYPOINT (tini) stays PID 1 and reaps the
    kubectl/gh grandchildren the tick spawns — same pattern as worker/pipeline.
    Overriding `command:` would drop tini → zombie accumulation → fork failure.
    Asserted against the parsed container, not a substring."""
    container = _monitor_container(manifest)
    assert container.get("args") == ["python3", "/app/monitor_command_server.py"], (
        "deile-monitor must run monitor_command_server.py via `args` (tini-wrapped)"
    )
    # `command:` MUST be absent so the image's tini ENTRYPOINT is not overridden.
    assert container.get("command") in (None, []), (
        "must NOT set `command:` (it would drop the tini ENTRYPOINT / zombie reaper)"
    )


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


def _monitor_env(manifest: str) -> dict:
    """Parse the deile-monitor container env list into a {name: value} dict."""
    env = {}
    for item in _monitor_container(manifest).get("env", []):
        if isinstance(item, dict) and "name" in item:
            env[item["name"]] = item.get("value")
    return env


def test_manifest_tick_interval_is_1800(manifest):
    assert _monitor_env(manifest).get("DEILE_MONITOR_TICK_INTERVAL_S") == "1800"


def test_manifest_caps_tool_iterations(manifest):
    assert _monitor_env(manifest).get("DEILE_MAX_TOOL_ITERATIONS") == "50"


def test_manifest_caps_qa_concurrency(manifest):
    # A Q&A turn spawns a full DEILE agent; concurrency must be pinned to 1 so a
    # burst can't OOM the pod alongside the tick + Phase-B subprocesses.
    assert _monitor_env(manifest).get("DEILE_MONITOR_QA_MAX_CONCURRENT") == "1"


def test_manifest_has_liveness_reflecting_tick(manifest):
    # A wedged tick behind a healthy HTTP server must be restartable.
    container = _monitor_container(manifest)
    live = container.get("livenessProbe")
    assert live and live["httpGet"]["path"] == "/v1/health", (
        "deile-monitor needs a livenessProbe on /v1/health (returns 503 on stale tick)"
    )


def test_manifest_bot_mounts_monitor_bearer():
    """The BOT pod must mount monitor-bearer or it can't authenticate to :8769."""
    import yaml as _yaml
    bot = (_REPO / "infra" / "k8s" / "manifests" / "20-bot-deployment.yaml").read_text(encoding="utf-8")
    docs = [d for d in _yaml.safe_load_all(bot) if d and d.get("kind") == "Deployment"]
    container = docs[0]["spec"]["template"]["spec"]["containers"][0]
    mounts = {m["name"]: m["mountPath"] for m in container.get("volumeMounts", [])}
    assert mounts.get("monitor-bearer") == "/run/secrets/bot/monitor", (
        "bot must mount monitor-bearer at /run/secrets/bot/monitor (else MONITOR_AUTH_MISSING)"
    )
    vols = {v["name"] for v in docs[0]["spec"]["template"]["spec"].get("volumes", [])}
    assert "monitor-bearer" in vols, "bot must declare the monitor-bearer volume"


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
