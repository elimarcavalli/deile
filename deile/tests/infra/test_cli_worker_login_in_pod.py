"""Testes do verb ``k8s cli-worker-login <kind> --in-pod`` (frota multi-CLI).

O ``--in-pod`` roda o device-auth DENTRO do pod (sem o CLI no host). Prova:

  * o parser de flags reconhece ``--in-pod`` (e mantém as demais flags);
  * o handler ``k8s_cli_worker_login`` com ``--in-pod`` delega para
    ``_k8s_in_pod_cli_worker_login`` (não para o ``bootstrap_cli_worker_oauth``
    host-capture);
  * o fluxo in-pod monta o Secret de credencial a partir do conteúdo LIDO do pod
    (``kubectl exec cat``), com a chave = basename do path da credencial;
  * o conteúdo da credencial NUNCA é materializado em log;
  * um worker SEM OAuthSpec é rejeitado (aponta o ``cli-worker-install``).

O CLI do ``deploy.py`` é hand-rolled (sem argparse); todo ``subprocess`` (kubectl)
e as etapas de cluster são mockados — nenhuma chamada real ao cluster.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
DEPLOY_PY = ROOT / "infra" / "k8s" / "deploy.py"


def _deploy_module():
    """Carrega o módulo ``deploy`` por path (não é pacote regular)."""
    sys.path.insert(0, str(DEPLOY_PY.parent))
    try:
        spec = importlib.util.spec_from_file_location("deploy_module_inpod", DEPLOY_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    finally:
        sys.path.pop(0)


# ===== parser de flags =======================================================


def test_parse_cli_worker_login_flags_in_pod():
    mod = _deploy_module()
    parsed = mod._parse_cli_worker_login_flags(["--in-pod"])
    assert parsed.get("in_pod") is True
    assert parsed.get("force_relogin") is False
    assert "_error" not in parsed


def test_parse_cli_worker_login_flags_in_pod_with_switch():
    mod = _deploy_module()
    parsed = mod._parse_cli_worker_login_flags(["--in-pod", "--switch"])
    assert parsed.get("in_pod") is True
    assert parsed.get("force_relogin") is True


def test_parse_cli_worker_login_flags_unknown_errors():
    mod = _deploy_module()
    parsed = mod._parse_cli_worker_login_flags(["--bogus"])
    assert "_error" in parsed


def test_actions_description_mentions_in_pod():
    mod = _deploy_module()
    desc = next((d for a, d in mod._K8S_ACTIONS if a == "cli-worker-login"), "")
    assert "--in-pod" in desc


# ===== delegação do handler para o fluxo in-pod ==============================


def test_handler_in_pod_delegates_to_in_pod_function():
    mod = _deploy_module()
    handler = mod._K8S["cli-worker-login"]
    calls = []

    def fake_in_pod(kind, *, ns, force_relogin=False):
        calls.append((kind, ns, force_relogin))
        return 0

    with patch.object(mod, "_k8s_in_pod_cli_worker_login", side_effect=fake_in_pod):
        rc = handler(
            {
                "extra": ["codex", "--in-pod"],
                "k8s_namespace": None,
                "yes": True,
                "dry_run": False,
            }
        )
    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == "codex"


# ===== fluxo in-pod — Secret montado do conteúdo lido, sem log do segredo =====


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_in_pod_builds_secret_from_read_content_no_secret_log(monkeypatch, caplog):
    mod = _deploy_module()
    secret_value = '{"tokens": {"access_token": "SUPER-SECRET-IN-POD"}}'

    applied_secrets: list = []

    # Stub das etapas de cluster reusadas do install/login.
    import _cli_worker_install as inst
    import _cli_worker_login as login

    monkeypatch.setattr(inst, "_kubectl_apply_keys_secret", lambda *a, **k: True)
    monkeypatch.setattr(inst, "_kubectl_sync_bearer", lambda *a, **k: True)
    monkeypatch.setattr(inst, "_kubectl_apply_manifest", lambda *a, **k: True)
    monkeypatch.setattr(inst, "_kubectl_scale", lambda *a, **k: True)
    monkeypatch.setattr(login, "_kubectl_set_auth_mode", lambda *a, **k: True)

    def fake_apply_cred(secret_name, payload, *, namespace):
        applied_secrets.append((secret_name, payload))
        return True

    monkeypatch.setattr(login, "_kubectl_apply_cred_secret", fake_apply_cred)

    # subprocess.run no deploy: get (running), exec login (rc=0), cat (cred),
    # rollout restart (rc=0).
    def fake_run(cmd, *a, **kw):
        joined = " ".join(str(c) for c in cmd)
        if "get" in cmd and "jsonpath" in joined:
            return _FakeCompleted(0, stdout="1")  # availableReplicas
        if "exec" in cmd and "cat" in cmd:
            return _FakeCompleted(0, stdout=secret_value)
        # device-auth exec (-it, sem capture) e rollout restart.
        return _FakeCompleted(0, stdout="ok")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    with caplog.at_level(logging.INFO):
        rc = mod._k8s_in_pod_cli_worker_login("codex", ns="deile")

    assert rc == 0
    # O Secret real (último apply) foi montado a partir do conteúdo lido, com a
    # chave = basename do cred path do codex (auth.json).
    assert applied_secrets, "nenhum Secret de credencial foi aplicado"
    last_name, last_payload = applied_secrets[-1]
    assert last_name == "codex-credentials"
    assert last_payload == {"auth.json": secret_value}
    # O conteúdo do segredo NUNCA aparece em log.
    assert "SUPER-SECRET-IN-POD" not in caplog.text


def test_in_pod_rejects_worker_without_oauthspec(monkeypatch):
    mod = _deploy_module()
    import cli_adapters

    class _NoOauth:
        kind = "noauthprobe2"
        auth_mode = "env"
        oauth = None
        auth_env_keys = ["OPENAI_API_KEY"]

    monkeypatch.setitem(cli_adapters.ADAPTERS, "noauthprobe2", _NoOauth())
    rc = mod._k8s_in_pod_cli_worker_login("noauthprobe2", ns="deile")
    assert rc == 64
