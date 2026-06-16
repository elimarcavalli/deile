"""Smoke test do verb `deploy.py k8s claude-login` (#309 fase 2, #335).

O CLI do `deploy.py` é hand-rolled (não usa argparse), portanto as
asserções abaixo refletem o real do verb:

  * O verbo `claude-login` é registrado em `_K8S` e listado em `_K8S_ACTIONS`.
  * `--no-interactive` sem credentials no host falha com exit !=0 e mensagem
    contendo "credentials" (vinda de `bootstrap_claude_worker`).
  * As flags `--switch` / `--force-relogin`, `--no-interactive` e `--in-pod`
    são parseadas a partir de `args["extra"]` pelo handler `k8s_claude_login`.
  * `--in-pod` delega para `_k8s_in_pod_claude_login` (issue #335).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEPLOY_PY = ROOT / "infra" / "k8s" / "deploy.py"


def _deploy_module():
    """Carrega o módulo `deploy` por path (não é pacote regular)."""
    import importlib.util

    sys.path.insert(0, str(DEPLOY_PY.parent))
    try:
        spec = importlib.util.spec_from_file_location("deploy_module", DEPLOY_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    finally:
        sys.path.pop(0)


def test_claude_login_is_registered_in_k8s_actions():
    """`claude-login` deve aparecer em `_K8S` (dispatch) e `_K8S_ACTIONS` (help/menu)."""
    mod = _deploy_module()
    assert "claude-login" in mod._K8S, (
        "verb `claude-login` não está registrado em `_K8S`; "
        "`deploy.py k8s claude-login` não vai despachar para o handler"
    )
    actions = {a for a, _ in mod._K8S_ACTIONS}
    assert "claude-login" in actions, (
        "verb `claude-login` não está em `_K8S_ACTIONS`; "
        "não vai aparecer em `deploy.py help` nem no menu"
    )


def test_claude_login_handler_parses_flags_from_extra():
    """O handler aceita --switch / --no-interactive a partir de `args['extra']`."""
    mod = _deploy_module()
    handler = mod._K8S["claude-login"]

    # Smoke: handler aceita as flags. Mocka bootstrap pra não tocar cluster.
    from unittest.mock import patch

    fake_result = type(
        "R",
        (),
        {
            "ok": True,
            "account_email": "user@test.com",
            "secret_applied": True,
            "deployment_applied": True,
            "rollout_ready": True,
            "error": None,
        },
    )()

    with patch("_claude_install.bootstrap_claude_worker", return_value=fake_result):
        rc = handler(
            {
                "extra": ["--switch", "--no-interactive"],
                "k8s_namespace": None,
                "yes": True,
                "dry_run": False,
            }
        )
    assert rc == 0


def test_claude_login_no_interactive_fails_without_creds(tmp_path):
    """Sem credentials no host + --no-interactive -> exit !=0 com erro claro."""
    env = {
        "HOME": str(tmp_path),  # ~/.claude/credentials.json ausente
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        [sys.executable, str(DEPLOY_PY), "k8s", "claude-login", "--no-interactive"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "credentials" in combined or "failed" in combined, (
        f"esperava menção a 'credentials'/'failed' no output; obtive:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Tests for --in-pod flag (issue #335)
# ---------------------------------------------------------------------------


def test_parse_claude_login_flags_in_pod():
    """``--in-pod`` é parseado em ``in_pod=True``."""
    mod = _deploy_module()
    result = mod._parse_claude_login_flags(["--in-pod"])
    assert result.get("in_pod") is True
    assert "_error" not in result


def test_parse_claude_login_flags_in_pod_with_others():
    """``--in-pod`` coexiste com outros flags (switch não faz sentido junto,
    mas o parser não valida combinações — isso fica na função que chama)."""
    mod = _deploy_module()
    result = mod._parse_claude_login_flags(["--in-pod"])
    assert result.get("in_pod") is True
    assert result.get("force_relogin") is False
    assert "_error" not in result


def test_parse_claude_login_flags_unknown_still_errors():
    """Flag desconhecida continua retornando ``_error``."""
    mod = _deploy_module()
    result = mod._parse_claude_login_flags(["--unknown-flag"])
    assert "_error" in result


def test_claude_login_in_pod_delegates_to_in_pod_function():
    """``k8s_claude_login`` com ``--in-pod`` invoca ``_k8s_in_pod_claude_login``
    e não ``bootstrap_claude_worker``."""
    mod = _deploy_module()
    handler = mod._K8S["claude-login"]

    calls = []

    def fake_in_pod(ns):
        calls.append(("in_pod", ns))
        return 0

    from unittest.mock import patch  # noqa: PLC0415

    with patch.object(mod, "_k8s_in_pod_claude_login", side_effect=fake_in_pod):
        rc = handler(
            {
                "extra": ["--in-pod"],
                "k8s_namespace": None,
                "yes": True,
                "dry_run": False,
            }
        )
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == "in_pod"


def test_claude_login_in_pod_registered_in_actions_description():
    """A descrição de ``claude-login`` em ``_K8S_ACTIONS`` menciona ``--in-pod``."""
    mod = _deploy_module()
    desc = next(
        (d for a, d in mod._K8S_ACTIONS if a == "claude-login"),
        "",
    )
    assert (
        "--in-pod" in desc
    ), f"esperava '--in-pod' na descrição de claude-login em _K8S_ACTIONS; obtive: {desc!r}"
