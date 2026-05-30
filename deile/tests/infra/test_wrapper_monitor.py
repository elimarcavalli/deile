"""Tests for ``wrapper.py`` monitor subcommand (issue #426).

Covers:
1. ``main()`` routes ``monitor`` role to ``_run_monitor``.
2. ``main()`` returns EX_USAGE (64) for unknown roles (updated error message).
3. ``_run_monitor`` exits 78 when no LLM key is present.
4. ``_run_monitor`` exits 78 when no forge token is present.
5. ``_install_monitor_negative_whitelist`` drops only ``dispatch_deile_task``.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    assert called.get("persona") == "monitor"
    assert "--persona" in called.get("argv", [])
    assert "monitor" in called.get("argv", [])

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
                        drop_values = {
                            elt.s for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                        }
    assert drop_values == {"dispatch_deile_task"}, (
        f"monitor DROP set should be exactly {{dispatch_deile_task}}, got {drop_values}"
    )
