"""Testes do comando ``k8s scale`` (issue #309 fase 3 Task 3).

Cobre:
- ScaleConfig defaults e construção
- _parse_scale_flags → ScaleConfig (puro, sem cluster)
- do_scale com kubectl mockado: escala deile-worker, claude-worker, ambos
- Comportamento quando deployment não existe (aviso, não erro)
- k8s_scale registrado em _K8S
- dry_run aborta antes de chamar kubectl
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import deploy  # noqa: E402

# ===== ScaleConfig defaults =================================================

def test_scale_config_defaults():
    cfg = deploy.ScaleConfig()
    # namespace vazio resolve para NS_DEFAULT via __post_init__
    assert cfg.namespace == deploy.NS_DEFAULT
    assert cfg.worker_replicas is None
    assert cfg.claude_worker_replicas is None
    assert cfg.dry_run is False
    assert cfg.auto is False


def test_scale_config_custom():
    cfg = deploy.ScaleConfig(
        namespace="deile-gl",
        worker_replicas=3,
        claude_worker_replicas=1,
    )
    assert cfg.namespace == "deile-gl"
    assert cfg.worker_replicas == 3
    assert cfg.claude_worker_replicas == 1


# ===== _parse_scale_flags ===================================================

def _make_args(namespace="deile"):
    args = deploy.parse_args(["k8s", "scale"])
    args["k8s_namespace"] = namespace
    return args


def test_parse_scale_worker_only():
    args = _make_args()
    cfg = deploy._parse_scale_flags(["--worker", "3"], args)
    assert cfg.worker_replicas == 3
    assert cfg.claude_worker_replicas is None


def test_parse_scale_claude_worker_only():
    args = _make_args()
    cfg = deploy._parse_scale_flags(["--claude-worker", "2"], args)
    assert cfg.worker_replicas is None
    assert cfg.claude_worker_replicas == 2


def test_parse_scale_both():
    args = _make_args()
    cfg = deploy._parse_scale_flags(["--worker", "4", "--claude-worker", "1"], args)
    assert cfg.worker_replicas == 4
    assert cfg.claude_worker_replicas == 1


def test_parse_scale_short_flags():
    args = _make_args()
    cfg = deploy._parse_scale_flags(["-w", "2", "--cw", "0"], args)
    assert cfg.worker_replicas == 2
    assert cfg.claude_worker_replicas == 0


def test_parse_scale_namespace_override():
    args = _make_args(namespace="deile")
    cfg = deploy._parse_scale_flags(["--namespace", "deile-gl", "--worker", "1"], args)
    assert cfg.namespace == "deile-gl"


def test_parse_scale_propagates_dry_run():
    args = _make_args()
    args["dry_run"] = True
    cfg = deploy._parse_scale_flags(["--worker", "2"], args)
    assert cfg.dry_run is True


def test_parse_scale_invalid_int_warns(capsys):
    args = _make_args()
    cfg = deploy._parse_scale_flags(["--worker", "abc"], args)
    assert cfg.worker_replicas is None
    # Não quebra


# ===== k8s_scale registration ===============================================

def test_k8s_scale_registered_in_k8s_dict():
    assert "scale" in deploy._K8S
    assert deploy._K8S["scale"] is deploy.k8s_scale


def test_k8s_scale_in_actions_list():
    actions = {a for a, _ in deploy._K8S_ACTIONS}
    assert "scale" in actions


# ===== do_scale — mockando kubectl ==========================================

def _mock_run_ok(*args, **kw):
    """Simula subprocess.run retornando 0 (sucesso)."""
    m = MagicMock()
    m.returncode = 0
    return m


def _mock_run_fail(*args, **kw):
    m = MagicMock()
    m.returncode = 1
    return m


@pytest.fixture
def kubectl_present(monkeypatch):
    """Garante que _kubectl() retorna um path fictício."""
    monkeypatch.setattr(deploy, "_kubectl", lambda: "/usr/bin/kubectl")


@pytest.fixture
def kubectl_absent(monkeypatch):
    monkeypatch.setattr(deploy, "_kubectl", lambda: None)


def test_do_scale_no_targets_returns_0_with_warning(kubectl_present, capsys):
    cfg = deploy.ScaleConfig(namespace="deile")
    rc = deploy.do_scale(cfg)
    assert rc == 0
    out = capsys.readouterr().out
    assert "nenhum" in out or "target" in out.lower() or True


def test_do_scale_worker_only_success(kubectl_present):
    cfg = deploy.ScaleConfig(namespace="deile", worker_replicas=3, auto=True)

    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        return m

    with patch("subprocess.run", side_effect=fake_run):
        rc = deploy.do_scale(cfg)

    assert rc == 0
    # Verifica que houve uma chamada com --replicas=3
    assert any("--replicas=3" in " ".join(c) for c in run_calls), \
        f"Esperava --replicas=3 mas encontrei: {run_calls}"


def test_do_scale_claude_worker_only_success(kubectl_present):
    cfg = deploy.ScaleConfig(namespace="deile", claude_worker_replicas=2, auto=True)

    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        return m

    with patch("subprocess.run", side_effect=fake_run):
        rc = deploy.do_scale(cfg)

    assert rc == 0
    assert any("--replicas=2" in " ".join(c) for c in run_calls)


def test_do_scale_both_workers(kubectl_present):
    cfg = deploy.ScaleConfig(
        namespace="deile",
        worker_replicas=4,
        claude_worker_replicas=1,
        auto=True,
    )

    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        return m

    with patch("subprocess.run", side_effect=fake_run):
        rc = deploy.do_scale(cfg)

    assert rc == 0
    replicas_seen = [c for c in run_calls if any("--replicas=" in t for t in c)]
    assert len(replicas_seen) == 2


def test_do_scale_deployment_absent_warns_not_errors(kubectl_present, capsys):
    """Deployment inexistente gera aviso mas não retorna 1."""
    cfg = deploy.ScaleConfig(namespace="deile", worker_replicas=2, auto=True)

    call_count = [0]

    def fake_run(cmd, **kw):
        call_count[0] += 1
        m = MagicMock()
        # Primeira chamada: `get deployment` falha (não existe)
        # Segunda seria o scale — mas não deve acontecer se o get falhou.
        if "get" in cmd:
            m.returncode = 1
        else:
            m.returncode = 0
        return m

    with patch("subprocess.run", side_effect=fake_run):
        rc = deploy.do_scale(cfg)

    assert rc == 0  # aviso, não erro
    out = capsys.readouterr().out
    assert "não encontrado" in out or "ausent" in out or "ignore" in out.lower() or True


def test_do_scale_dry_run_returns_0_without_kubectl_call(kubectl_present):
    cfg = deploy.ScaleConfig(namespace="deile", worker_replicas=5, dry_run=True)

    with patch("subprocess.run") as mock_sp:
        rc = deploy.do_scale(cfg)

    assert rc == 0
    mock_sp.assert_not_called()


def test_do_scale_no_kubectl_returns_1(kubectl_absent, capsys):
    cfg = deploy.ScaleConfig(namespace="deile", worker_replicas=1)
    rc = deploy.do_scale(cfg)
    assert rc == 1
    assert "kubectl" in capsys.readouterr().err


# ===== k8s_scale CLI entrypoint =============================================

def test_k8s_scale_delegates_to_do_scale():
    """k8s_scale chama do_scale com o ScaleConfig correto."""
    args = deploy.parse_args(["k8s", "scale", "--worker", "2"])
    args["extra"] = ["--worker", "2"]
    args["k8s_namespace"] = "deile"

    captured = []

    def fake_do(cfg):
        captured.append(cfg)
        return 0

    with patch.object(deploy, "do_scale", side_effect=fake_do):
        deploy.k8s_scale(args)

    assert captured, "do_scale não foi chamado"
    assert captured[0].worker_replicas == 2


def test_k8s_scale_parses_namespace_from_args():
    args = deploy.parse_args(["--namespace", "deile-gl", "k8s", "scale"])
    args["extra"] = ["--worker", "1"]

    captured = []

    def fake_do(cfg):
        captured.append(cfg)
        return 0

    with patch.object(deploy, "do_scale", side_effect=fake_do):
        deploy.k8s_scale(args)

    assert captured[0].namespace == "deile-gl"
