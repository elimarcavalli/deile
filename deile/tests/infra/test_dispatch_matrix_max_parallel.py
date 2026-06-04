"""Testes de max_parallel e hotkey [c] cleanup no DispatchMatrixView (issue #408).

Cobre:
- Render: linha "Max Parallel" aparece após "Worker Scaling".
- Cursor pode atingir Max Parallel (N+2) e é clamped lá.
- [enter] na linha Max Parallel abre prompt max_parallel_prompt.
- [p] abre prompt max_parallel_prompt independente de cursor.
- _handle_max_parallel_prompt_key: dígitos, [a]=auto, [backspace], [enter], [esc].
- _apply_max_parallel: demo mode (no-op), sem kubectl (erro), sucesso, value=None (clear).
- Hotkey [c]: abre cleanup_confirm modal (modo demo).
- _handle_cleanup_confirm_key: Y confirma, N/ESC cancela.
- Render: modal cleanup_confirm e max_parallel_prompt aparecem no output.
- Settings: DEILE_PIPELINE_MAX_PARALLEL env var registrada.
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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def view_demo():
    from _panel import DispatchMatrixView
    return DispatchMatrixView(data=None)


@pytest.fixture
def view_with_data():
    from _panel import DispatchMatrixView
    from _panel_data import ClaudeWorkerStatus, StageDispatchEntry

    data = MagicMock()
    data.stage_dispatch.get_all_stages.return_value = [
        StageDispatchEntry(s, "deile-worker", None, "default")
        for s in ("classify", "refine", "implement", "pr_review", "follow_ups")
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
    return MagicMock()


# ---------------------------------------------------------------------------
# Render: "Max Parallel" row aparece no output
# ---------------------------------------------------------------------------

def test_render_contains_max_parallel_row(view_demo, app_stub):
    from rich.console import Console
    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    assert "Max Parallel" in out, \
        f"'Max Parallel' não encontrado na saída:\n{out[:600]}"


def test_render_max_parallel_after_worker_scaling(view_demo, app_stub):
    from rich.console import Console
    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    idx_scaling = out.find("Worker Scaling")
    idx_mp = out.find("Max Parallel")
    assert idx_scaling != -1, "'Worker Scaling' não encontrado"
    assert idx_mp != -1, "'Max Parallel' não encontrado"
    assert idx_mp > idx_scaling, "'Max Parallel' deve aparecer APÓS 'Worker Scaling'"


def test_render_max_parallel_shows_default(view_demo, app_stub):
    from rich.console import Console
    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    # Em modo demo sem cluster, a linha deve mostrar o valor default
    assert "default" in out.lower() or "DEILE_PIPELINE_MAX_PARALLEL" in out


# ---------------------------------------------------------------------------
# Navegação: cursor pode atingir Max Parallel (N+2)
# ---------------------------------------------------------------------------

def test_navigation_down_reaches_max_parallel_row(view_demo, app_stub):
    from _panel import ActionResult
    n_stages = len(view_demo._stages())
    max_parallel_idx = n_stages + 2

    # Navega até N+2
    for _ in range(max_parallel_idx):
        result = view_demo.handle_key("DOWN", app_stub)
        assert isinstance(result, ActionResult)

    assert view_demo.cursor_row == max_parallel_idx


def test_navigation_down_clamps_at_max_parallel_row(view_demo, app_stub):
    """DOWN não ultrapassa N+3 (Monitor é a última linha, após Max Parallel)."""
    n_stages = len(view_demo._stages())
    # Ordem: stages... Global(+0) Scaling(+1) MaxParallel(+2) Monitor(+3)
    target = n_stages + 3

    for _ in range(30):
        view_demo.handle_key("DOWN", app_stub)

    assert view_demo.cursor_row == target


def test_navigation_up_from_max_parallel_reaches_scaling(view_demo, app_stub):
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 2  # Max Parallel

    view_demo.handle_key("UP", app_stub)

    assert view_demo.cursor_row == n_stages + 1  # Worker Scaling


# ---------------------------------------------------------------------------
# [enter] na linha Max Parallel → abre max_parallel_prompt
# ---------------------------------------------------------------------------

def test_enter_on_max_parallel_row_opens_prompt(view_demo, app_stub):
    n_stages = len(view_demo._stages())
    view_demo.cursor_row = n_stages + 2

    view_demo.handle_key("\r", app_stub)

    assert view_demo.mode is not None
    kind, _, _ = view_demo.mode
    assert kind == "max_parallel_prompt"


# ---------------------------------------------------------------------------
# [p] abre max_parallel_prompt independente de cursor
# ---------------------------------------------------------------------------

def test_p_hotkey_opens_max_parallel_prompt(view_demo, app_stub):
    view_demo.cursor_row = 0  # em qualquer linha

    view_demo.handle_key("p", app_stub)

    assert view_demo.mode is not None
    kind, _, _ = view_demo.mode
    assert kind == "max_parallel_prompt"


# ---------------------------------------------------------------------------
# _handle_max_parallel_prompt_key
# ---------------------------------------------------------------------------

def test_max_parallel_prompt_digit_input(view_demo, app_stub):
    view_demo._open_max_parallel_prompt()
    assert view_demo.mode is not None

    view_demo.handle_key("4", app_stub)

    _, _, opts = view_demo.mode
    assert "4" in opts[0]


def test_max_parallel_prompt_multi_digit(view_demo, app_stub):
    view_demo._open_max_parallel_prompt()

    view_demo.handle_key("1", app_stub)
    view_demo.handle_key("0", app_stub)

    _, _, opts = view_demo.mode
    assert opts[0] == "10"


def test_max_parallel_prompt_a_sets_auto(view_demo, app_stub):
    view_demo._open_max_parallel_prompt()

    view_demo.handle_key("a", app_stub)

    _, _, opts = view_demo.mode
    assert opts[0] == "auto"


def test_max_parallel_prompt_backspace(view_demo, app_stub):
    view_demo._open_max_parallel_prompt()
    view_demo.handle_key("5", app_stub)
    view_demo.handle_key("BACKSPACE", app_stub)

    _, _, opts = view_demo.mode
    assert opts[0] == ""


def test_max_parallel_prompt_esc_cancels(view_demo, app_stub):
    view_demo._open_max_parallel_prompt()

    view_demo.handle_key("ESC", app_stub)

    assert view_demo.mode is None
    assert "cancel" in (view_demo.last_msg or "").lower()


def test_max_parallel_prompt_enter_applies_demo(view_demo, app_stub):
    """Em demo, enter com '3' fecha modal e registra last_msg."""
    view_demo._open_max_parallel_prompt()
    view_demo.handle_key("3", app_stub)

    view_demo.handle_key("\r", app_stub)

    assert view_demo.mode is None
    assert view_demo.last_msg is not None


def test_max_parallel_prompt_enter_empty_applies_clear(view_demo, app_stub):
    """Enter com buffer vazio chama _apply_max_parallel(None)."""
    view_demo._open_max_parallel_prompt()
    # Buffer começa vazio (demo mode → _read_max_parallel_env → "")

    view_demo.handle_key("\r", app_stub)

    assert view_demo.mode is None


# ---------------------------------------------------------------------------
# _apply_max_parallel
# ---------------------------------------------------------------------------

def test_apply_max_parallel_demo_mode(view_demo):
    view_demo._apply_max_parallel("3")
    assert view_demo.last_ok is False  # demo → False
    assert "demo" in (view_demo.last_msg or "").lower()


def test_apply_max_parallel_no_kubectl(view_with_data):
    with patch("_panel.kubectl_bin", return_value=None):
        view_with_data._apply_max_parallel("2")
    assert view_with_data.last_ok is False
    assert "kubectl" in (view_with_data.last_msg or "").lower()


def test_apply_max_parallel_invalid_value(view_with_data):
    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"):
        view_with_data._apply_max_parallel("0")  # < 1 → inválido
    assert view_with_data.last_ok is False
    assert "inválido" in (view_with_data.last_msg or "").lower()


def test_apply_max_parallel_success(view_with_data):

    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        return m

    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run", side_effect=fake_run):
        view_with_data._apply_max_parallel("5")

    assert view_with_data.last_ok is True
    assert "5" in (view_with_data.last_msg or "")


def test_apply_max_parallel_auto(view_with_data):

    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        return m

    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run", side_effect=fake_run):
        view_with_data._apply_max_parallel("auto")

    assert view_with_data.last_ok is True
    assert "auto" in (view_with_data.last_msg or "")


def test_apply_max_parallel_clear(view_with_data):
    """value=None → remove o override."""

    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        return m

    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run", side_effect=fake_run):
        view_with_data._apply_max_parallel(None)

    assert view_with_data.last_ok is True
    assert "default" in (view_with_data.last_msg or "").lower()


# ---------------------------------------------------------------------------
# Hotkey [c] — cleanup on-demand
# ---------------------------------------------------------------------------

def test_c_hotkey_opens_cleanup_confirm(view_demo, app_stub):
    view_demo.handle_key("c", app_stub)

    assert view_demo.mode is not None
    kind, _, _ = view_demo.mode
    assert kind == "cleanup_confirm"


def test_cleanup_confirm_esc_cancels(view_demo, app_stub):
    view_demo.handle_key("c", app_stub)
    assert view_demo.mode is not None

    view_demo.handle_key("ESC", app_stub)

    assert view_demo.mode is None
    assert "cancel" in (view_demo.last_msg or "").lower()


def test_cleanup_confirm_n_cancels(view_demo, app_stub):
    view_demo.handle_key("c", app_stub)
    view_demo.handle_key("N", app_stub)

    assert view_demo.mode is None


def test_cleanup_confirm_y_triggers_cleanup_demo(view_demo, app_stub):
    """Em modo demo, Y fecha o modal e atualiza last_msg."""
    view_demo.handle_key("c", app_stub)

    view_demo.handle_key("Y", app_stub)

    assert view_demo.mode is None
    assert view_demo.last_msg is not None


def test_cleanup_confirm_default_cursor_is_no(view_demo, app_stub):
    """Picker default deve apontar para 'Não' (índice 1) — safe default."""
    view_demo.handle_key("c", app_stub)
    assert view_demo.picker_cursor == 1  # "Não (N)"


# ---------------------------------------------------------------------------
# Render: modais aparecem no output
# ---------------------------------------------------------------------------

def test_render_cleanup_confirm_modal_visible(view_demo, app_stub):
    from rich.console import Console
    view_demo.handle_key("c", app_stub)

    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    assert "CLEANUP" in out.upper() or "cleanup" in out.lower()


def test_render_max_parallel_prompt_modal_visible(view_demo, app_stub):
    from rich.console import Console
    view_demo.handle_key("p", app_stub)

    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    assert "MAX_PARALLEL" in out.upper() or "parallel" in out.lower()


# ---------------------------------------------------------------------------
# Settings: DEILE_PIPELINE_MAX_PARALLEL env var registrada
# ---------------------------------------------------------------------------

def test_settings_has_pipeline_max_parallel_env_var(monkeypatch):
    """DEILE_PIPELINE_MAX_PARALLEL deve ser aplicado via _apply_env_overrides.

    NÃO recriar o módulo ``deile.config.settings`` via ``del sys.modules`` +
    re-import: isso forja uma SEGUNDA cópia do módulo com seu próprio singleton
    ``_settings``, enquanto módulos que já ligaram ``get_settings`` no topo
    (``dispatch_resolver``, ``deile_md_loader``) continuam apontando para a
    cópia original — o teste muta uma cópia e o código lê a outra (pollution de
    ordenação confirmada em #499). ``reset_settings()`` já garante uma instância
    fresca sem duplicar o módulo.
    """
    from deile.config.settings import (_apply_env_overrides, get_settings,
                                       reset_settings)

    monkeypatch.setenv("DEILE_PIPELINE_MAX_PARALLEL", "7")
    reset_settings()

    settings_obj = get_settings()
    # Aplica overrides manualmente para o teste não depender de ordem de init
    _apply_env_overrides(settings_obj)

    assert settings_obj.pipeline_max_parallel == 7
