"""Helpers kubectl centralizados (``_kubectl_helpers``) — refator DRY 2026-06-10.

O padrão ``kubectl create secret ... --dry-run=client -o yaml | kubectl apply``
e a leitura ``kubectl get secret -o jsonpath`` + base64-decode, antes duplicados
entre ``_cli_worker_install.py`` e ``_cli_worker_login.py``, foram centralizados
em ``infra/k8s/_kubectl_helpers.py``. Estes testes cobrem os caminhos que só
eram exercitados indiretamente: merge de chaves existentes, branch não-fatal de
``sync_bearer_secret`` (source ausente → ``True``), edge cases de I/O e — pilar
08 — a garantia de que NENHUM valor de secret aparece em log.

``subprocess.run`` é mockado: nenhum kubectl real é invocado.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _kubectl_helpers as kh  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _router(*, get=None, dry=None, apply=None, raises=None):
    """Constrói um fake de ``subprocess.run`` que roteia por tipo de comando.

    ``raises`` (exception) é levantada SEMPRE, simulando timeout/binário ausente.
    """

    def _fake(argv, **kwargs):
        if raises is not None:
            raise raises
        if "get" in argv:
            return get if get is not None else _FakeCompleted(returncode=1)
        if "--dry-run=client" in argv:
            return dry if dry is not None else _FakeCompleted(stdout="yaml: doc")
        if "apply" in argv:
            return apply if apply is not None else _FakeCompleted()
        raise AssertionError(f"argv inesperado: {argv}")

    return _fake


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


# --------------------------------------------------------------------------- #
# apply_generic_secret
# --------------------------------------------------------------------------- #
def test_apply_empty_literals_is_noop_false(monkeypatch):
    """``literals`` vazio → ``False`` SEM invocar kubectl (guarda preservada)."""

    def _boom(*a, **k):
        raise AssertionError("kubectl não deveria ser chamado")

    monkeypatch.setattr(kh.subprocess, "run", _boom)
    assert kh.apply_generic_secret("s", {}, namespace="deile") is False


def test_apply_happy_path(monkeypatch):
    monkeypatch.setattr(kh.subprocess, "run", _router())
    assert kh.apply_generic_secret("s", {"K": "V"}, namespace="deile") is True


def test_apply_dry_run_failure_returns_false(monkeypatch):
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(dry=_FakeCompleted(returncode=1, stderr="boom")),
    )
    assert kh.apply_generic_secret("s", {"K": "V"}, namespace="deile") is False


def test_apply_apply_failure_returns_false(monkeypatch):
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(apply=_FakeCompleted(returncode=1, stderr="apply boom")),
    )
    assert kh.apply_generic_secret("s", {"K": "V"}, namespace="deile") is False


def test_apply_timeout_returns_false(monkeypatch):
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(raises=subprocess.TimeoutExpired(cmd="kubectl", timeout=15)),
    )
    assert kh.apply_generic_secret("s", {"K": "V"}, namespace="deile") is False


def test_apply_kubectl_missing_returns_false(monkeypatch):
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(raises=FileNotFoundError("kubectl")),
    )
    assert kh.apply_generic_secret("s", {"K": "V"}, namespace="deile") is False


def test_apply_never_logs_secret_value(monkeypatch, caplog):
    """Pilar 08: o VALOR do secret nunca entra em log, mesmo em falha."""
    secret_value = "super-secret-token-xyz"
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(dry=_FakeCompleted(returncode=1, stderr="kubectl error sem valor")),
    )
    with caplog.at_level(logging.DEBUG):
        kh.apply_generic_secret("s", {"K": secret_value}, namespace="deile")
    assert secret_value not in caplog.text


# --------------------------------------------------------------------------- #
# read_secret_value
# --------------------------------------------------------------------------- #
def test_read_value_decodes_base64(monkeypatch):
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(get=_FakeCompleted(stdout=_b64("tok-123"))),
    )
    assert kh.read_secret_value("sec", "AUTH_TOKEN", namespace="deile") == "tok-123"


def test_read_value_absent_returns_none(monkeypatch):
    """Secret/chave ausente (stdout vazio) → ``None`` silencioso."""
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(get=_FakeCompleted(returncode=0, stdout="")),
    )
    assert kh.read_secret_value("sec", "AUTH_TOKEN", namespace="deile") is None


def test_read_value_invalid_base64_returns_none(monkeypatch, caplog):
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(get=_FakeCompleted(stdout="!!!not-base64!!!")),
    )
    with caplog.at_level(logging.ERROR):
        assert kh.read_secret_value("sec", "K", namespace="deile") is None


def test_read_value_timeout_returns_none(monkeypatch):
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(raises=subprocess.TimeoutExpired(cmd="kubectl", timeout=15)),
    )
    assert kh.read_secret_value("sec", "K", namespace="deile") is None


# --------------------------------------------------------------------------- #
# read_secret_data_map
# --------------------------------------------------------------------------- #
def test_read_map_decodes_all_and_skips_invalid(monkeypatch):
    """Merge use-case: decodifica todas as chaves, pula base64 inválido."""
    data = {"A": _b64("alpha"), "B": "!!!bad!!!", "C": _b64("gamma")}
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(get=_FakeCompleted(stdout=json.dumps(data))),
    )
    assert kh.read_secret_data_map("sec", namespace="deile") == {
        "A": "alpha",
        "C": "gamma",
    }


def test_read_map_absent_returns_none(monkeypatch):
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(get=_FakeCompleted(returncode=1)),
    )
    assert kh.read_secret_data_map("sec", namespace="deile") is None


def test_read_map_non_dict_payload_returns_none(monkeypatch):
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(get=_FakeCompleted(stdout="[1,2,3]")),
    )
    assert kh.read_secret_data_map("sec", namespace="deile") is None


# --------------------------------------------------------------------------- #
# sync_bearer_secret
# --------------------------------------------------------------------------- #
def test_sync_bearer_source_absent_is_non_fatal_true(monkeypatch, caplog):
    """Source ausente → ``True`` (não-fatal; rollout fica pending) + warning."""
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(get=_FakeCompleted(returncode=1)),
    )
    with caplog.at_level(logging.WARNING):
        ok = kh.sync_bearer_secret(
            source_secret="worker-bearer",
            source_key="AUTH_TOKEN",
            target_secret="x-bearer",
            target_key="CLI_WORKER_BEARER_TOKEN",
            namespace="deile",
        )
    assert ok is True
    assert "worker-bearer" in caplog.text


def test_sync_bearer_copies_token_and_never_logs_it(monkeypatch, caplog):
    """Source presente → copia o token ao target; o token nunca é logado."""
    token = "bearer-secret-abc"
    monkeypatch.setattr(
        kh.subprocess,
        "run",
        _router(get=_FakeCompleted(stdout=_b64(token))),
    )
    with caplog.at_level(logging.DEBUG):
        ok = kh.sync_bearer_secret(
            source_secret="worker-bearer",
            source_key="AUTH_TOKEN",
            target_secret="x-bearer",
            target_key="CLI_WORKER_BEARER_TOKEN",
            namespace="deile",
        )
    assert ok is True
    assert token not in caplog.text
