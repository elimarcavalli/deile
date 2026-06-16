"""Frente 6 — tela dedicada de Worker Scaling (réplicas por tipo de worker).

A edição de réplicas saiu da ``DispatchMatrixView`` (tela [d]) e ganhou a
``WorkerScalingView`` (tela [S]): uma linha por worker (os dispatchers do
registro de adapters), com [+/-] aplicando scale na hora e [enter] abrindo
prompt numérico. O scale reusa ``_scale_deployment`` (sem duplicar a chamada
kubectl) e nunca toca o cluster nos testes (mock).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))


@pytest.fixture
def view_demo():
    from _panel import WorkerScalingView

    return WorkerScalingView(data=None)


@pytest.fixture
def view_with_data():
    from _panel import WorkerScalingView

    data = MagicMock()
    data.context = MagicMock()
    data.context.namespace = "deile"
    data.pods = MagicMock()
    data.pods.get.return_value = []
    return WorkerScalingView(data=data)


@pytest.fixture
def app_stub():
    return MagicMock()


def _render(view, app) -> str:
    console = Console(width=140)
    with console.capture() as cap:
        console.print(view.render(app))
    return cap.get()


# --------------------------------------------------------------------------- #
# Render — lista todos os workers do registro
# --------------------------------------------------------------------------- #


def test_render_lists_all_fleet_workers(view_demo, app_stub):
    from deile.orchestration.pipeline.dispatch_resolver import get_valid_dispatchers

    out = _render(view_demo, app_stub)
    assert "WORKER SCALING" in out
    for dispatcher in get_valid_dispatchers():
        assert dispatcher in out, f"{dispatcher} ausente da tela de scaling"


def test_render_shows_desired_and_ready_columns(view_demo, app_stub):
    out = _render(view_demo, app_stub)
    assert "Desired" in out and "Ready" in out


# --------------------------------------------------------------------------- #
# Navegação
# --------------------------------------------------------------------------- #


def test_navigation_wraps(view_demo, app_stub):
    n = len(view_demo._worker_deployments())
    assert n >= 2
    view_demo.cursor = 0
    view_demo.handle_key("UP", app_stub)
    assert view_demo.cursor == n - 1  # wrap
    view_demo.handle_key("DOWN", app_stub)
    assert view_demo.cursor == 0


def test_q_and_esc_return_to_dashboard(view_demo, app_stub):
    from _panel import Action

    for key in ("q", "ESC"):
        r = view_demo.handle_key(key, app_stub)
        assert r.kind == Action.NAV and r.target == "dashboard"


# --------------------------------------------------------------------------- #
# [+]/[-] aplicam scale via _scale_deployment
# --------------------------------------------------------------------------- #


def test_plus_increments_and_scales(view_with_data, app_stub):
    import _panel

    view_with_data.cursor = 0
    deploy = view_with_data._worker_deployments()[0]
    with (
        patch.object(_panel, "_read_deployment_replicas", return_value=2) as rd,
        patch.object(_panel, "_scale_deployment", return_value=(True, "ok")) as sc,
    ):
        view_with_data.handle_key("+", app_stub)
    rd.assert_called_once()
    # target = 2 + 1 = 3 no deployment selecionado.
    sc.assert_called_once_with("deile", deploy, 3)
    assert view_with_data.last_ok is True


def test_minus_clamps_at_zero(view_with_data, app_stub):
    import _panel

    view_with_data.cursor = 0
    with (
        patch.object(_panel, "_read_deployment_replicas", return_value=0),
        patch.object(_panel, "_scale_deployment") as sc,
    ):
        view_with_data.handle_key("-", app_stub)
    # Já em 0 → não chama scale (clamp), mostra info.
    sc.assert_not_called()
    assert view_with_data.last_ok is None


# --------------------------------------------------------------------------- #
# [enter] → prompt numérico de valor exato
# --------------------------------------------------------------------------- #


def test_enter_opens_numeric_prompt(view_with_data, app_stub):
    view_with_data.cursor = 0
    view_with_data.handle_key("\r", app_stub)
    assert view_with_data.mode is not None
    assert view_with_data.mode[0] == "scale"


def test_prompt_digits_and_enter_applies(view_with_data, app_stub):
    import _panel

    view_with_data.cursor = 0
    deploy = view_with_data._worker_deployments()[0]
    view_with_data.handle_key("\r", app_stub)  # abre prompt
    view_with_data.handle_key("5", app_stub)
    assert view_with_data.mode[2] == ["5"]
    with patch.object(_panel, "_scale_deployment", return_value=(True, "ok")) as sc:
        view_with_data.handle_key("\r", app_stub)
    sc.assert_called_once_with("deile", deploy, 5)
    assert view_with_data.mode is None  # prompt fechou


def test_prompt_esc_cancels_without_scaling(view_with_data, app_stub):
    import _panel

    view_with_data.cursor = 0
    view_with_data.handle_key("\r", app_stub)
    with patch.object(_panel, "_scale_deployment") as sc:
        view_with_data.handle_key("ESC", app_stub)
    sc.assert_not_called()
    assert view_with_data.mode is None


def test_prompt_clamps_to_max(view_with_data, app_stub):
    import _panel

    view_with_data.cursor = 0
    deploy = view_with_data._worker_deployments()[0]
    view_with_data.handle_key("\r", app_stub)
    view_with_data.handle_key("9", app_stub)
    view_with_data.handle_key("9", app_stub)  # buffer "99"
    with patch.object(_panel, "_scale_deployment", return_value=(True, "ok")) as sc:
        view_with_data.handle_key("\r", app_stub)
    # 99 → clamp em _MAX_REPLICAS (20).
    sc.assert_called_once_with("deile", deploy, view_with_data._MAX_REPLICAS)


# --------------------------------------------------------------------------- #
# Demo mode — nunca toca cluster
# --------------------------------------------------------------------------- #


def test_demo_mode_no_scale_call(view_demo, app_stub):
    import _panel

    view_demo.cursor = 0
    with patch.object(_panel, "_scale_deployment") as sc:
        view_demo.handle_key("+", app_stub)
    sc.assert_not_called()
    assert view_demo.last_ok is False
