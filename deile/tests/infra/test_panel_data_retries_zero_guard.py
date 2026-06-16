"""Tests for the zero-retries guard in ``_panel_data.set_stage_retries``.

Guard rationale: historicamente alguém setou
``DEILE_PIPELINE_RETRIES_IMPLEMENT=0`` no Deployment achando que ``0``
significava "default" ou "infinito". Na verdade, ``0`` é semanticamente
"fail-fast — primeira falha bloqueia o stage" — e o pipeline ficou
travado sem aviso. O guard exige ``allow_zero=True`` para aceitar ``0``;
o widget repassa esse flag quando o operador digita ``"0!"`` (bang
explícito) em vez de só ``"0"``.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_INFRA_K8S = os.path.join(os.path.dirname(__file__), "..", "..", "..", "infra", "k8s")
if _INFRA_K8S not in sys.path:
    sys.path.insert(0, _INFRA_K8S)


@pytest.fixture
def fresh_panel_data(monkeypatch):
    """Garante carregamento real do módulo ``_panel_data``, sem stub mínimo de
    sibling tests. Usa ``monkeypatch.delitem`` para que ``sys.modules`` seja
    **restaurado automaticamente no teardown** — caso contrário o módulo
    real ficaria cacheado em ``sys.modules`` e arquivos vizinhos (cujo
    fixture autouse só injeta o stub quando ``"_panel_data" not in
    sys.modules``) passariam a ver o módulo real, quebrando 17 testes em
    batch. Bug identificado por @deile-one no review da PR #407.
    """
    monkeypatch.delitem(sys.modules, "_panel_data", raising=False)
    import importlib

    pd = importlib.import_module("_panel_data")
    return pd


def _completed_proc(returncode=0, stdout="rollout", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def test_zero_rejected_without_allow_zero(fresh_panel_data):
    pd = fresh_panel_data
    monkey = patch.object(pd, "kubectl_bin", return_value="/fake/kubectl")
    with monkey, patch.object(pd.subprocess, "run") as run:
        ok, msg = pd.set_stage_retries("implement", 0)
    assert ok is False
    assert "fail-fast" in msg
    assert "0!" in msg
    run.assert_not_called()


def test_zero_accepted_with_allow_zero_true(fresh_panel_data):
    pd = fresh_panel_data
    with (
        patch.object(pd, "kubectl_bin", return_value="/fake/kubectl"),
        patch.object(pd.subprocess, "run", return_value=_completed_proc()),
    ):
        ok, msg = pd.set_stage_retries("implement", 0, allow_zero=True)
    assert ok is True
    assert "DEILE_PIPELINE_RETRIES_IMPLEMENT=0" in msg


def test_positive_retries_unaffected_by_guard(fresh_panel_data):
    """Valor positivo sempre aceito — guard só toca em 0."""
    pd = fresh_panel_data
    with (
        patch.object(pd, "kubectl_bin", return_value="/fake/kubectl"),
        patch.object(pd.subprocess, "run", return_value=_completed_proc()),
    ):
        ok, msg = pd.set_stage_retries("implement", 3)
    assert ok is True
    assert "RETRIES_IMPLEMENT=3" in msg


def test_none_unaffected_by_guard(fresh_panel_data):
    """``None`` (clear) sempre aceito — guard só toca em 0."""
    pd = fresh_panel_data
    with (
        patch.object(pd, "kubectl_bin", return_value="/fake/kubectl"),
        patch.object(
            pd.subprocess, "run", return_value=_completed_proc(stdout="unset ok")
        ),
    ):
        ok, msg = pd.set_stage_retries("implement", None)
    assert ok is True
    assert "unset" in msg


def test_negative_rejected_before_zero_guard(fresh_panel_data):
    """Validação ``< 0`` precede o guard de zero — mensagem original mantida."""
    pd = fresh_panel_data
    with (
        patch.object(pd, "kubectl_bin", return_value="/fake/kubectl"),
        patch.object(pd.subprocess, "run") as run,
    ):
        ok, msg = pd.set_stage_retries("implement", -1)
    assert ok is False
    assert ">= 0" in msg
    run.assert_not_called()


def test_invalid_stage_rejected_before_zero_guard(fresh_panel_data):
    """Validação de stage canônico precede o guard de zero."""
    pd = fresh_panel_data
    with (
        patch.object(pd, "kubectl_bin", return_value="/fake/kubectl"),
        patch.object(pd.subprocess, "run") as run,
    ):
        ok, msg = pd.set_stage_retries("not_a_stage", 0)
    assert ok is False
    assert "invalid stage" in msg
    run.assert_not_called()


def test_audit_emitted_on_zero_denied(fresh_panel_data):
    """Zero rejeitado emite audit ``denied`` (rastreabilidade do bloqueio)."""
    pd = fresh_panel_data
    with (
        patch.object(pd, "kubectl_bin", return_value="/fake/kubectl"),
        patch.object(pd.subprocess, "run"),
        patch.object(pd, "_audit_timeout_retries_change") as audit,
    ):
        pd.set_stage_retries("implement", 0)
    audit.assert_called_once()
    kw = audit.call_args.kwargs
    assert kw.get("result") == "denied"
    assert "zero rejeitado" in kw.get("detail", "")
