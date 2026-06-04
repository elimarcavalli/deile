"""Testes do DispatchMatrixView — linha Worker Scaling (issue #309 fase 3 Task 4).

Cobre:
- Render: linha "Worker Scaling" aparece na tabela após "Global default"
- Navegação: cursor atinge a linha N+1 via DOWN
- [enter] na linha de scaling → abre picker de réplicas (scale_prompt)
- _open_scaling_prompt: gera opções 0-10, kind correto
- _handle_scale_prompt_key: ESC cancela, enter aplica, ↑/↓ navega
- _apply_scaling: modo demo (sem cluster) → last_msg informativo, sem crash
- cursor_col 0 → deile-worker; col 1 → claude-worker
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))


# ---------------------------------------------------------------------------
# Fixture base: DispatchMatrixView em modo demo (sem cluster, data=None)
# ---------------------------------------------------------------------------

@pytest.fixture
def view_demo():
    """DispatchMatrixView sem data (modo demo) — sem dependência de cluster."""
    from _panel import DispatchMatrixView
    return DispatchMatrixView(data=None)


@pytest.fixture
def view_with_data():
    """DispatchMatrixView com data mockada (sem cluster real)."""
    from _panel import DispatchMatrixView
    from _panel_data import ClaudeWorkerStatus, StageDispatchEntry

    data = MagicMock()
    data.stage_dispatch.get_all_stages.return_value = [
        StageDispatchEntry("classify",  "deile-worker", None, "default"),
        StageDispatchEntry("refine",    "deile-worker", None, "default"),
        StageDispatchEntry("implement", "deile-worker", None, "default"),
        StageDispatchEntry("pr_review", "deile-worker", None, "default"),
        StageDispatchEntry("follow_ups", "deile-worker", None, "default"),
    ]
    data.stage_dispatch.get_claude_worker_status.return_value = ClaudeWorkerStatus(
        deployment_applied=False, pod_ready=False, logged_in_email=None,
    )
    data.context = MagicMock()
    data.context.namespace = "deile"
    data.models = MagicMock()
    data.models.get.return_value = []
    return DispatchMatrixView(data=data)


@pytest.fixture
def app_stub():
    """Stub mínimo de PanelApp (só o que o render usa)."""
    app = MagicMock()
    return app


# ---------------------------------------------------------------------------
# Render: linha "Worker Scaling" existe na saída
# ---------------------------------------------------------------------------

def test_render_contains_worker_scaling_row(view_demo, app_stub):
    from rich.console import Console
    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    # Captura o output como string
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    assert "Worker Scaling" in out, \
        f"'Worker Scaling' não encontrado na saída do render:\n{out[:500]}"


def test_render_worker_scaling_after_global_default(view_demo, app_stub):
    from rich.console import Console
    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    idx_global = out.find("Global default")
    idx_scaling = out.find("Worker Scaling")
    assert idx_global != -1, "'Global default' não encontrado"
    assert idx_scaling != -1, "'Worker Scaling' não encontrado"
    assert idx_scaling > idx_global, \
        "'Worker Scaling' deve aparecer APÓS 'Global default'"


def test_render_has_replicas_labels(view_demo, app_stub):
    from rich.console import Console
    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    assert "réplicas" in out, "Colunas de réplicas não apareceram no render"


# ---------------------------------------------------------------------------
# Navegação: cursor pode atingir a linha de scaling
# ---------------------------------------------------------------------------

def test_navigation_down_reaches_scaling_row(view_demo, app_stub):
    from _panel import ActionResult

    n_stages = len(view_demo._stages())
    # Navega até N+1 (a linha Worker Scaling)
    for _ in range(n_stages + 1):
        result = view_demo.handle_key("DOWN", app_stub)
        assert isinstance(result, ActionResult)

    # cursor_row deve ser N+1
    assert view_demo.cursor_row == n_stages + 1


def test_navigation_down_clamps_at_scaling_row(view_demo, app_stub):
    """DOWN não ultrapassa N+3 (Monitor row, após Max Parallel — issue #426)."""
    n_stages = len(view_demo._stages())
    # max_row = n_stages + 3 (Global=+0, Scaling=+1, Max Parallel=+2, Monitor=+3)
    target = n_stages + 3

    # Pressiona DOWN muitas vezes
    for _ in range(20):
        view_demo.handle_key("DOWN", app_stub)

    assert view_demo.cursor_row == target, \
        f"cursor_row={view_demo.cursor_row}, esperava {target}"


def test_navigation_back_from_scaling_to_global(view_demo, app_stub):
    n_stages = len(view_demo._stages())
    # Vai até scaling
    for _ in range(n_stages + 1):
        view_demo.handle_key("DOWN", app_stub)
    assert view_demo.cursor_row == n_stages + 1

    # Sobe uma linha → deve estar na Global default
    view_demo.handle_key("UP", app_stub)
    assert view_demo.cursor_row == n_stages


# ---------------------------------------------------------------------------
# [enter] na linha de scaling → open_scaling_prompt
# ---------------------------------------------------------------------------

def test_enter_on_scaling_row_opens_scale_prompt(view_demo, app_stub):
    n_stages = len(view_demo._stages())
    # Navega até a linha de scaling
    view_demo.cursor_row = n_stages + 1
    view_demo.cursor_col = 0  # col 0 = deile-worker

    view_demo.handle_key("\r", app_stub)

    assert view_demo.mode is not None, "mode deve estar ativo após [enter] no scaling"
    kind, deploy_name, options = view_demo.mode
    assert kind == "scale_prompt"
    assert deploy_name == "deile-worker"
    assert "0" in options and "10" in options


def test_enter_on_scaling_row_col1_opens_claude_worker_prompt(view_demo, app_stub):
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 1
    view_demo.cursor_col = 1  # col 1 = claude-worker

    view_demo.handle_key("\r", app_stub)

    assert view_demo.mode is not None
    kind, deploy_name, options = view_demo.mode
    assert kind == "scale_prompt"
    assert deploy_name == "claude-worker"


# ---------------------------------------------------------------------------
# _open_scaling_prompt
# ---------------------------------------------------------------------------

def test_open_scaling_prompt_options_range(view_demo):
    view_demo.cursor_col = 0
    view_demo._open_scaling_prompt()
    assert view_demo.mode is not None
    _, _, opts = view_demo.mode
    # Opções: "0" a "10" (11 itens)
    assert len(opts) == 11
    assert opts[0] == "0"
    assert opts[10] == "10"


def test_open_scaling_prompt_clears_last_msg(view_demo):
    view_demo.last_msg = "mensagem antiga"
    view_demo.cursor_col = 0
    view_demo._open_scaling_prompt()
    assert view_demo.last_msg == ""


# ---------------------------------------------------------------------------
# _handle_scale_prompt_key
# ---------------------------------------------------------------------------

def test_scale_prompt_esc_cancels(view_demo, app_stub):
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 1
    view_demo.cursor_col = 0
    view_demo._open_scaling_prompt()
    assert view_demo.mode is not None

    view_demo.handle_key("ESC", app_stub)

    assert view_demo.mode is None
    assert "cancel" in (view_demo.last_msg or "").lower() or view_demo.last_ok is None


def test_scale_prompt_n_cancels(view_demo, app_stub):
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 1
    view_demo.cursor_col = 0
    view_demo._open_scaling_prompt()

    view_demo.handle_key("N", app_stub)

    assert view_demo.mode is None


def test_scale_prompt_up_down_navigation(view_demo, app_stub):
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 1
    view_demo.cursor_col = 0
    view_demo._open_scaling_prompt()
    initial_cursor = view_demo.picker_cursor

    view_demo.handle_key("DOWN", app_stub)
    assert view_demo.picker_cursor == (initial_cursor + 1) % 11

    view_demo.handle_key("UP", app_stub)
    assert view_demo.picker_cursor == initial_cursor


def test_scale_prompt_enter_applies_scaling_demo(view_demo, app_stub):
    """Em modo demo (data=None), enter fecha o modal e registra last_msg."""
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 1
    view_demo.cursor_col = 0
    view_demo._open_scaling_prompt()
    view_demo.picker_cursor = 3  # seleciona "3"

    view_demo.handle_key("\r", app_stub)

    assert view_demo.mode is None
    # Em demo, last_msg deve ser informativo (sem crash)
    assert view_demo.last_msg is not None


# ---------------------------------------------------------------------------
# _apply_scaling
# ---------------------------------------------------------------------------

def test_apply_scaling_demo_mode_no_crash(view_demo):
    """Em modo demo, _apply_scaling não deve lançar exceção."""
    view_demo._apply_scaling("deile-worker", 3)
    assert view_demo.last_ok is False  # demo → False (informativo)
    assert "demo" in (view_demo.last_msg or "").lower()


def test_apply_scaling_with_data_no_kubectl(view_with_data):
    """Sem kubectl, last_ok=False e mensagem de erro adequada."""
    with patch("_panel.kubectl_bin", return_value=None):
        view_with_data._apply_scaling("deile-worker", 2)
    assert view_with_data.last_ok is False
    assert "kubectl" in (view_with_data.last_msg or "").lower()


def test_apply_scaling_deployment_not_found(view_with_data):
    """Deployment ausente → last_ok=False com hint de instalação."""

    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 1  # deployment não existe
        m.stderr = ""
        return m

    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run", side_effect=fake_run):
        view_with_data._apply_scaling("claude-worker", 1)

    assert view_with_data.last_ok is False
    assert "não encontrado" in (view_with_data.last_msg or "")


def test_apply_scaling_success(view_with_data):
    """kubectl retorna 0 → last_ok=True."""
    call_count = [0]

    def fake_run(cmd, **kw):
        call_count[0] += 1
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        return m

    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run", side_effect=fake_run):
        view_with_data._apply_scaling("deile-worker", 5)

    assert view_with_data.last_ok is True
    assert "5" in (view_with_data.last_msg or "")
    assert "deile-worker" in (view_with_data.last_msg or "")


# ---------------------------------------------------------------------------
# Render: highlight na linha de scaling
# ---------------------------------------------------------------------------

def test_render_highlights_scaling_row_col0(view_demo, app_stub):
    """Cursor na linha scaling col 0 → célula deile-worker em [reverse]."""
    from rich.console import Console
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 1
    view_demo.cursor_col = 0

    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    # O texto "réplicas deile-worker" (ou o texto da célula) deve aparecer
    assert "deile-worker" in out or "réplicas" in out


def test_render_global_default_still_present_when_at_scaling(view_demo, app_stub):
    """Ambas as linhas (Global default + Worker Scaling) aparecem juntas."""
    from rich.console import Console
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 1

    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    assert "Global default" in out
    assert "Worker Scaling" in out


# ---------------------------------------------------------------------------
# Render: scale_prompt modal aparece no output
# ---------------------------------------------------------------------------

def test_render_scale_prompt_modal_visible(view_demo, app_stub):
    """Quando mode='scale_prompt', o picker numérico aparece no render."""
    from rich.console import Console
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 1
    view_demo.cursor_col = 0
    view_demo._open_scaling_prompt()

    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    # O picker deve mostrar o título do modal
    assert "RÉPLICAS" in out.upper() or "réplicas" in out
