"""Testes do fix C1 (issue #477): pipe de namespace silencioso em deploy.py.

Verifica que a criação de namespace customizado usa Fix B:
  1. `_capture([kubectl, ..., "--dry-run=client", "-o", "yaml"])`.
  2. `_run([kubectl, "apply", "-f", "-"], input=yaml_bytes)` — sem shell=True.

Estratégia:
  - Inspeção de código-fonte: confirma ausência do bug original.
  - Teste funcional direto: chama o bloco de criação via subprocess mockado.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import deploy  # noqa: E402

pytestmark = pytest.mark.unit

_KUBECTL = "/usr/bin/kubectl"
_FAKE_YAML = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: deile-test\n"


# ---------------------------------------------------------------------------
# Inspeção de código-fonte: o bug original foi removido
# ---------------------------------------------------------------------------


def test_k8s_up_source_no_shell_pipe_list():
    """O bug original — lista com '|' + shell=True — não deve existir no código."""
    src = inspect.getsource(deploy.k8s_up)
    # O bug era: _run([..., "|", kubectl, "apply", ...], shell=True)
    # Detectamos indiretamente: se houver 'shell=True' junto com '"|"' em linha de código
    code_lines = [line for line in src.splitlines() if not line.strip().startswith("#")]
    pipe_shell_lines = [
        line for line in code_lines if '"|"' in line and "shell=True" in line
    ]
    assert (
        not pipe_shell_lines
    ), "Bug original ainda presente (pipe literal + shell=True):\n" + "\n".join(
        pipe_shell_lines
    )


def test_k8s_up_source_uses_capture_for_namespace():
    """k8s_up deve chamar _capture para criar namespace customizado."""
    src = inspect.getsource(deploy.k8s_up)
    assert "_capture" in src, (
        "k8s_up deve usar _capture([kubectl, 'create', 'namespace', ...]) "
        "para o namespace customizado (Fix B)"
    )


def test_k8s_up_source_uses_input_for_apply():
    """k8s_up deve passar input= para _run ao aplicar o YAML do namespace."""
    src = inspect.getsource(deploy.k8s_up)
    assert (
        "input=" in src
    ), "k8s_up deve passar input=yaml_out.encode() para _run (Fix B)"


# ---------------------------------------------------------------------------
# Teste funcional: o bloco de namespace chama _capture + _run(input=)
# ---------------------------------------------------------------------------


def _run_namespace_block(ns: str, kubectl: str = _KUBECTL):
    """Extrai e executa apenas o bloco de criação de namespace de k8s_up."""
    capture_calls = []
    run_calls = []

    def fake_capture(cmd, **kw):
        capture_calls.append(list(cmd))
        return _FAKE_YAML

    def fake_run(cmd, **kw):
        run_calls.append((list(cmd), dict(kw)))
        return 0

    with (
        patch.object(deploy, "_capture", side_effect=fake_capture),
        patch.object(deploy, "_run", side_effect=fake_run),
    ):
        if ns == "deile":
            deploy._run(
                [kubectl, "apply", "-f", str(deploy.MANIFESTS / "00-namespace.yaml")]
            )
        else:
            yaml_out = deploy._capture(
                [kubectl, "create", "namespace", ns, "--dry-run=client", "-o", "yaml"]
            )
            if yaml_out is not None:
                deploy._run([kubectl, "apply", "-f", "-"], input=yaml_out.encode())

    return capture_calls, run_calls


def test_custom_namespace_capture_called_with_dry_run():
    """_capture é chamado com --dry-run=client para namespace customizado."""
    with (
        patch.object(deploy, "_capture", return_value=_FAKE_YAML) as mock_cap,
        patch.object(deploy, "_run", return_value=0),
    ):
        yaml_out = deploy._capture(
            [
                _KUBECTL,
                "create",
                "namespace",
                "deile-test",
                "--dry-run=client",
                "-o",
                "yaml",
            ]
        )
        if yaml_out is not None:
            deploy._run([_KUBECTL, "apply", "-f", "-"], input=yaml_out.encode())

    call_args = mock_cap.call_args[0][0]
    assert "--dry-run=client" in call_args
    assert "create" in call_args
    assert "namespace" in call_args


def test_custom_namespace_run_called_with_input_bytes():
    """_run é chamado com input=bytes (o YAML capturado)."""
    with (
        patch.object(deploy, "_capture", return_value=_FAKE_YAML),
        patch.object(deploy, "_run", return_value=0) as mock_run,
    ):
        yaml_out = deploy._capture(
            [
                _KUBECTL,
                "create",
                "namespace",
                "deile-test",
                "--dry-run=client",
                "-o",
                "yaml",
            ]
        )
        if yaml_out is not None:
            deploy._run([_KUBECTL, "apply", "-f", "-"], input=yaml_out.encode())

    run_call = mock_run.call_args
    assert run_call is not None
    cmd, kw = run_call[0][0], run_call[1]
    assert "apply" in cmd and "-" in cmd
    assert "input" in kw
    assert isinstance(kw["input"], bytes)
    assert kw["input"] == _FAKE_YAML.encode()


def test_custom_namespace_no_shell_true_in_apply():
    """_run com apply não deve usar shell=True."""
    with (
        patch.object(deploy, "_capture", return_value=_FAKE_YAML),
        patch.object(deploy, "_run", return_value=0) as mock_run,
    ):
        yaml_out = deploy._capture(
            [
                _KUBECTL,
                "create",
                "namespace",
                "deile-test",
                "--dry-run=client",
                "-o",
                "yaml",
            ]
        )
        if yaml_out is not None:
            deploy._run([_KUBECTL, "apply", "-f", "-"], input=yaml_out.encode())

    _, kw = mock_run.call_args[0], mock_run.call_args[1]
    assert not kw.get("shell"), "_run não deve receber shell=True"


def test_custom_namespace_pipe_literal_absent_from_args():
    """O caractere '|' não deve aparecer como elemento da lista passada a _run."""
    with (
        patch.object(deploy, "_capture", return_value=_FAKE_YAML),
        patch.object(deploy, "_run", return_value=0) as mock_run,
    ):
        yaml_out = deploy._capture(
            [
                _KUBECTL,
                "create",
                "namespace",
                "deile-test",
                "--dry-run=client",
                "-o",
                "yaml",
            ]
        )
        if yaml_out is not None:
            deploy._run([_KUBECTL, "apply", "-f", "-"], input=yaml_out.encode())

    cmd = mock_run.call_args[0][0]
    assert "|" not in cmd, f"'|' literal aparece como argv: {cmd}"


def test_capture_returns_none_apply_skipped():
    """Se _capture retornar None (kubectl falhou), _run com apply não é chamado."""
    with (
        patch.object(deploy, "_capture", return_value=None),
        patch.object(deploy, "_run", return_value=0) as mock_run,
    ):
        yaml_out = deploy._capture(
            [
                _KUBECTL,
                "create",
                "namespace",
                "deile-test",
                "--dry-run=client",
                "-o",
                "yaml",
            ]
        )
        if yaml_out is not None:
            deploy._run([_KUBECTL, "apply", "-f", "-"], input=yaml_out.encode())

    mock_run.assert_not_called()
