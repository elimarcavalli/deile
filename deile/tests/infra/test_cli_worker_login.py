"""Unit tests para _cli_worker_login — captura OAuth do host + install (frota CLI).

Espelha os testes do ``_claude_install`` para o fluxo genérico
``deploy.py k8s cli-worker-login <kind>``. Prova:

* resolução do path da credencial no host (``~`` e env var de home do CLI);
* montagem do payload do Secret a partir do conteúdo (chave = basename), sem
  materializar o valor em log;
* idempotência / fail-fast (sem credencial + non-interactive → erro claro);
* erro claro para ``kind`` sem ``auth_mode=oauth_file`` (aponta o env-auth).

Sem rede / sem cluster: o subprocess do kubectl e o ``login_cmd`` são mockados.
O pacote vive em ``infra/k8s/`` — path inserido manualmente (convenção infra).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

WORKER_DIR = Path(__file__).resolve().parents[3] / "infra" / "k8s"


@pytest.fixture
def login_mod():
    for p in (WORKER_DIR.parent, WORKER_DIR):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    import importlib

    import _cli_worker_login

    importlib.reload(_cli_worker_login)
    return _cli_worker_login


@pytest.fixture
def codex_adapter():
    if str(WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(WORKER_DIR))
    import cli_adapters

    return cli_adapters.get_adapter("codex")


# ===== resolução do path da credencial no host ===============================


class TestResolveHostCredPath:
    def test_expands_tilde_to_home(self, login_mod, codex_adapter):
        path = login_mod.resolve_host_cred_path(
            "codex", codex_adapter, env={"HOME": "/opr"}, home=Path("/opr"),
        )
        assert path == Path("/opr/.codex/auth.json")

    def test_codex_home_env_var_overrides(self, login_mod, codex_adapter):
        # CODEX_HOME setado no host → a credencial fica sob ele (basename).
        path = login_mod.resolve_host_cred_path(
            "codex", codex_adapter,
            env={"HOME": "/opr", "CODEX_HOME": "/custom/codex"},
        )
        assert path == Path("/custom/codex/auth.json")

    def test_none_when_adapter_has_no_oauth(self, login_mod):
        class _NoOauth:
            oauth = None

        assert login_mod.resolve_host_cred_path("x", _NoOauth(), env={}) is None


# ===== payload do Secret a partir do conteúdo (sem log do segredo) ===========


class TestBuildCredSecretPayload:
    def test_key_is_basename_of_cred_path(self, login_mod):
        payload = login_mod.build_cred_secret_payload(
            '{"tokens": {}}', cred_path=Path("/h/.codex/auth.json"),
        )
        assert payload == {"auth.json": '{"tokens": {}}'}

    def test_read_host_credential_does_not_log_secret(
        self, login_mod, tmp_path, caplog,
    ):
        secret = '{"tokens": {"access_token": "SUPER-SECRET-VALUE"}}'
        cred = tmp_path / "auth.json"
        cred.write_text(secret, encoding="utf-8")
        with caplog.at_level(logging.INFO):
            content = login_mod.read_host_credential(cred)
        assert content == secret
        # O valor do segredo nunca aparece no log — só o comprimento.
        assert "SUPER-SECRET-VALUE" not in caplog.text
        assert f"len={len(secret)}" in caplog.text

    def test_read_host_credential_none_when_absent(self, login_mod, tmp_path):
        assert login_mod.read_host_credential(tmp_path / "nope.json") is None


# ===== bootstrap_cli_worker_oauth — guardas e idempotência ===================


class TestBootstrapGuards:
    def test_unknown_kind_returns_error(self, login_mod):
        res = login_mod.bootstrap_cli_worker_oauth("does-not-exist")
        assert not res.ok
        assert res.error

    def test_env_auth_kind_rejected_points_to_install(self, login_mod):
        # codex default é auth_mode=env → cli-worker-login deve recusar e
        # apontar o cli-worker-install (erro claro, paridade com o inverso).
        res = login_mod.bootstrap_cli_worker_oauth("codex")
        assert not res.ok
        assert "cli-worker-install" in (res.error or "")

    def test_no_credential_non_interactive_fails_fast(
        self, login_mod, codex_adapter, monkeypatch, tmp_path,
    ):
        # Força o adapter resolvido a ter auth_mode oauth_file (opt-in do codex),
        # sem credencial no host e interactive=False → fail-fast com erro claro.
        import cli_adapters

        oauth_codex = _shallow_oauth_clone(codex_adapter)
        monkeypatch.setitem(cli_adapters.ADAPTERS, "codex", oauth_codex)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CODEX_HOME", raising=False)

        res = login_mod.bootstrap_cli_worker_oauth(
            "codex", interactive=False, home=tmp_path,
        )
        assert not res.ok
        assert "interactive=False" in (res.error or "")


def _shallow_oauth_clone(adapter):
    """Clona o adapter codex forçando ``auth_mode='oauth_file'`` (opt-in)."""
    import copy

    clone = copy.copy(adapter)
    object.__setattr__(clone, "auth_mode", "oauth_file")
    return clone
