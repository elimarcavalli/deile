"""Tests for ``wrapper.py`` monitor subcommand (issue #426).

Covers:
1. ``main()`` routes ``monitor`` role to ``_run_monitor``.
2. ``main()`` returns EX_USAGE (64) for unknown roles (updated error message).
3. ``_run_monitor`` exits 78 when no LLM key is present.
4. ``_run_monitor`` exits 78 when no forge token is present.
5. ``_install_monitor_negative_whitelist`` drops only ``dispatch_deile_task``.
6. Deployment 55-deile-monitor uses a shell-loop tick driver and exposes
   ``DEILE_MONITOR_TICK_INTERVAL_S`` (no naked interactive CLI).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def wrapper_mod():
    """Load ``infra/k8s/wrapper.py`` dynamically (same pattern as other wrapper tests)."""
    repo_root = Path(__file__).resolve().parents[3]
    wrapper_path = repo_root / "infra" / "k8s" / "wrapper.py"
    spec = importlib.util.spec_from_file_location(
        "wrapper_under_test_monitor", str(wrapper_path),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wrapper_under_test_monitor"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_main_routes_monitor_role(wrapper_mod, tmp_path, monkeypatch):
    """main() with ``monitor`` calls _run_monitor and returns its exit code."""
    monkeypatch.setenv("HOME", str(tmp_path))
    called = {}

    def fake_run_monitor(rest):
        called["rest"] = rest
        return 0

    monkeypatch.setattr(wrapper_mod, "_run_monitor", fake_run_monitor)
    rc = wrapper_mod.main(["wrapper.py", "monitor", "--some-arg"])
    assert rc == 0
    assert called["rest"] == ["--some-arg"]


def test_main_unknown_role_mentions_monitor(wrapper_mod, capsys):
    """Unknown role error message must list 'monitor' in the expected set."""
    rc = wrapper_mod.main(["wrapper.py", "unknown-role"])
    assert rc == 64
    captured = capsys.readouterr()
    assert "monitor" in captured.err


# ---------------------------------------------------------------------------
# _run_monitor: auth guards
# ---------------------------------------------------------------------------

def test_run_monitor_exits_78_no_llm_key(wrapper_mod, tmp_path, monkeypatch, capsys):
    """Returns 78 when no LLM API key is present."""
    monkeypatch.setenv("HOME", str(tmp_path))
    secrets_dir = tmp_path / "run" / "secrets" / "deile"
    secrets_dir.mkdir(parents=True)
    # No *_API_KEY files

    def fake_load_secrets(path):
        return {}  # empty — no LLM keys

    def fake_harden():
        pass

    monkeypatch.setattr(wrapper_mod, "_load_secret_files", fake_load_secrets)
    monkeypatch.setattr(wrapper_mod, "_harden_runtime_dirs", fake_harden)

    rc = wrapper_mod._run_monitor([])
    assert rc == 78
    assert "no *_API_KEY" in capsys.readouterr().err


def test_run_monitor_exits_78_no_forge_token(wrapper_mod, tmp_path, monkeypatch, capsys):
    """Returns 78 when LLM key is present but no forge token."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Remove any inherited forge tokens
    for var in ("GITHUB_TOKEN", "GITLAB_TOKEN", "GL_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    def fake_load_secrets(path):
        return {"ANTHROPIC_API_KEY": "sk-test"}

    def fake_harden():
        pass

    monkeypatch.setattr(wrapper_mod, "_load_secret_files", fake_load_secrets)
    monkeypatch.setattr(wrapper_mod, "_harden_runtime_dirs", fake_harden)
    # _has_llm_key must return True for this secret set
    monkeypatch.setattr(wrapper_mod, "_has_llm_key", lambda loaded: True)

    rc = wrapper_mod._run_monitor([])
    assert rc == 78
    captured = capsys.readouterr()
    assert "GITHUB_TOKEN" in captured.err or "GITLAB_TOKEN" in captured.err


def test_run_monitor_starts_with_monitor_persona(wrapper_mod, tmp_path, monkeypatch):
    """With valid credentials, sets DEILE_DEFAULT_PERSONA=monitor and calls deile_main."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    def fake_load_secrets(path):
        return {"ANTHROPIC_API_KEY": "sk-test", "GITHUB_TOKEN": "ghp_test"}

    def fake_harden():
        pass

    def fake_setup_forge():
        pass

    def fake_patch_bootstrap():
        pass

    def fake_install_whitelist():
        pass

    called = {}

    def fake_deile_main():
        called["persona"] = os.environ.get("DEILE_DEFAULT_PERSONA")
        called["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(wrapper_mod, "_load_secret_files", fake_load_secrets)
    monkeypatch.setattr(wrapper_mod, "_harden_runtime_dirs", fake_harden)
    monkeypatch.setattr(wrapper_mod, "_has_llm_key", lambda loaded: True)
    monkeypatch.setattr(wrapper_mod, "_setup_forge_credentials", fake_setup_forge)
    monkeypatch.setattr(wrapper_mod, "_patch_deile_bootstrap", fake_patch_bootstrap)
    monkeypatch.setattr(wrapper_mod, "_install_monitor_negative_whitelist", fake_install_whitelist)

    # Patch the deile.cli import
    fake_cli_mod = MagicMock()
    fake_cli_mod.main = fake_deile_main
    sys.modules["deile.cli"] = fake_cli_mod

    rc = wrapper_mod._run_monitor([])
    assert rc == 0
    # Persona é selecionada via env var (o CLI deile não tem flag --persona).
    assert called.get("persona") == "monitor"
    argv = called.get("argv", [])
    assert argv[0] == "deile"
    assert "--persona" not in argv  # garantia que removemos a flag inválida

    del sys.modules["deile.cli"]


# ---------------------------------------------------------------------------
# _install_monitor_negative_whitelist
# ---------------------------------------------------------------------------

def test_install_monitor_whitelist_drops_only_dispatch(wrapper_mod):
    """Only dispatch_deile_task is dropped from the monitor's DROP set.

    Tests the DROP constant directly — the patching mechanism is shared with
    _install_worker_negative_whitelist (already tested in other tests). We
    verify that the monitor's DROP set is exactly {"dispatch_deile_task"} and
    that bash/file tools are NOT in it.
    """
    import inspect
    src = inspect.getsource(wrapper_mod._install_monitor_negative_whitelist)
    # The DROP set must contain dispatch_deile_task
    assert "dispatch_deile_task" in src
    # The DROP set must NOT contain bash or file tools (those are kept)
    assert "bash_execute" not in src
    assert "read_file" not in src
    assert "write_file" not in src
    # Verify by extracting the DROP set from the function
    # (it's defined as a literal set in the function body)
    import ast
    tree = ast.parse(src)
    drop_values = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DROP":
                    if isinstance(node.value, ast.Set):
                        # ast.Constant é a API moderna (Python 3.8+); .value substitui o .s
                        # de ast.Str (deprecated em 3.12 e removido em 3.14).
                        drop_values = {
                            elt.value for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                        }
    assert drop_values == {"dispatch_deile_task"}, (
        f"monitor DROP set should be exactly {{dispatch_deile_task}}, got {drop_values}"
    )


# ---------------------------------------------------------------------------
# Deployment manifest: tick driver
# ---------------------------------------------------------------------------

def _load_monitor_deployment():
    """Parse 55-deile-monitor-deployment.yaml into a list of documents."""
    import yaml
    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / "infra" / "k8s" / "manifests" / "55-deile-monitor-deployment.yaml"
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    return [d for d in docs if d]


def test_monitor_deployment_uses_shell_loop_tick_driver():
    """The monitor pod must drive ticks via an explicit loop, not a bare CLI call.

    Regression for issue raised in PR #430 reviews: an args of
    ``["python3", "/app/wrapper.py", "monitor"]`` (no positional message and no
    surrounding loop) drops into the interactive DEILE CLI, which on a pod
    without TTY either blocks on stdin or exits immediately — neither runs the
    tick loop described in monitor.md. The fix is a shell loop wrapping
    one-shot DEILE invocations.
    """
    docs = _load_monitor_deployment()
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    command = container.get("command") or []
    args = container.get("args") or []
    joined = " ".join(command) + " " + " ".join(args)

    # Must explicitly invoke a shell (otherwise there is no loop construct)
    assert any("/bin/sh" in c or "/bin/bash" in c for c in command), (
        f"monitor container must use a shell to drive the tick loop; "
        f"got command={command!r}"
    )
    # Loop primitive and the wrapper invocation must both be present
    assert "while" in joined and "sleep" in joined, (
        "monitor args must contain a 'while ... sleep' loop (the tick driver); "
        f"got args={args!r}"
    )
    assert "wrapper.py" in joined and "monitor" in joined, (
        "monitor loop must invoke wrapper.py monitor each tick; "
        f"got args={args!r}"
    )
    # Tick interval must be configurable at runtime via env (not baked in)
    assert "DEILE_MONITOR_TICK_INTERVAL_S" in joined, (
        "monitor loop must honor ${DEILE_MONITOR_TICK_INTERVAL_S} for runtime "
        f"override; got args={args!r}"
    )
    env_names = {e["name"] for e in container.get("env", []) if "name" in e}
    assert "DEILE_MONITOR_TICK_INTERVAL_S" in env_names, (
        "monitor deployment must declare DEILE_MONITOR_TICK_INTERVAL_S in env "
        f"(with a default); got env names={sorted(env_names)}"
    )


def test_monitor_serviceaccount_automount_aligns_with_pod():
    """SA and podTemplate must agree on automountServiceAccountToken.

    Both must allow the token (true) — the monitor's kubectl calls require it.
    Divergent values (SA=false, pod=true) audit-confusingly even when the pod
    spec wins; alignment removes the inconsistency.
    """
    docs = _load_monitor_deployment()
    sa = next(d for d in docs if d.get("kind") == "ServiceAccount")
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    assert sa.get("automountServiceAccountToken") is True, (
        "deile-monitor-sa must set automountServiceAccountToken: true to "
        "match the podTemplate (which needs the token for kubectl calls)"
    )
    assert pod_spec.get("automountServiceAccountToken") is True, (
        "podTemplate must set automountServiceAccountToken: true; the monitor "
        "uses kubectl for OAuth renewal and pod cleanup"
    )
