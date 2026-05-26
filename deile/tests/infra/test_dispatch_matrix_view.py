"""DispatchMatrixView render + skeleton handlers (#309 fase 2 Task 18).

Cobertura mínima do skeleton da nova view unificada que (na Task 21) substitui
``DispatchModeView`` (PR #330, global flip) + ``StageModelsView`` (#305,
per-stage model). Esta task cobre apenas:

- Render: 5 stages + linha "Global default" + header com status do
  ``claude-worker`` (logado como email / NÃO INSTALADO).
- Navegação básica: ↑↓ row, ←→ col, q → back.

Pickers / actions ([enter] edit, [r] reset, [L] login switch, [I] install)
ficam como STUBS implementados nas Tasks 19-20.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# infra/k8s não é um package — adicionar a sys.path para ``_panel_data`` /
# ``_panel`` resolverem (mirror de como ``deploy.py panel`` invoca o módulo).
_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))


@pytest.fixture
def mock_data_with_claude():
    """PanelData mockada com StageDispatchProvider que reporta claude-worker
    instalado + ready + email."""
    data = MagicMock()
    from _panel_data import ClaudeWorkerStatus, StageDispatchEntry

    data.stage_dispatch.get_all_stages.return_value = [
        StageDispatchEntry("classify", "deile-worker",
                           "anthropic:haiku", "env"),
        StageDispatchEntry("refine", "deile-worker",
                           "anthropic:sonnet", "env"),
        StageDispatchEntry("implement", "claude-worker",
                           "anthropic:claude-opus-4-7", "env"),
        StageDispatchEntry("pr_review", "claude-worker",
                           "anthropic:claude-opus-4-7", "env"),
        StageDispatchEntry("follow_ups", "deile-worker", None, "default"),
    ]
    data.stage_dispatch.get_claude_worker_status.return_value = (
        ClaudeWorkerStatus(
            deployment_applied=True,
            pod_ready=True,
            logged_in_email="user@example.com",
        )
    )
    data.namespace = "deile"
    return data


@pytest.fixture
def mock_data_no_claude():
    """PanelData mockada com claude-worker NÃO INSTALADO — todos os stages
    caem em default deile-worker."""
    data = MagicMock()
    from _panel_data import ClaudeWorkerStatus, StageDispatchEntry

    data.stage_dispatch.get_all_stages.return_value = [
        StageDispatchEntry(s, "deile-worker", None, "default")
        for s in ("classify", "refine", "implement", "pr_review", "follow_ups")
    ]
    data.stage_dispatch.get_claude_worker_status.return_value = (
        ClaudeWorkerStatus(
            deployment_applied=False,
            pod_ready=False,
            logged_in_email=None,
        )
    )
    data.namespace = "deile"
    return data


def test_view_renders_5_stages(mock_data_with_claude):
    from _panel import DispatchMatrixView
    from rich.console import Console

    view = DispatchMatrixView(data=mock_data_with_claude)
    console = Console(width=140, record=True)
    rendered = view.render(None)
    console.print(rendered)
    text = console.export_text()

    # All 5 stages visible.
    for stage in ("classify", "refine", "implement", "pr_review",
                  "follow_ups"):
        assert stage in text, f"stage {stage} missing from render"

    # Worker values visible.
    assert "claude-worker" in text
    assert "deile-worker" in text

    # Pelo menos um model slug visível.
    assert ("anthropic:claude-opus-4-7" in text
            or "claude-opus-4-7" in text)


def test_view_shows_claude_worker_ready_in_header(mock_data_with_claude):
    from _panel import DispatchMatrixView
    from rich.console import Console

    view = DispatchMatrixView(data=mock_data_with_claude)
    console = Console(width=140, record=True)
    console.print(view.render(None))
    text = console.export_text()

    # Email logado deve aparecer (ou pelo menos a palavra "user").
    assert "user@example.com" in text or "user" in text.lower()
    # Status do pod deve aparecer como ready/ok.
    assert "ready" in text.lower() or "ok" in text.lower()


def test_view_shows_install_hint_when_claude_absent(mock_data_no_claude):
    from _panel import DispatchMatrixView
    from rich.console import Console

    view = DispatchMatrixView(data=mock_data_no_claude)
    console = Console(width=140, record=True)
    console.print(view.render(None))
    text = console.export_text()

    # Deve hintar a action de install — "instalar" PT ou "install" EN ou "[I]".
    assert ("instalar" in text.lower()
            or "install" in text.lower()
            or "[i]" in text.lower())


def test_view_navigation_keys_update_cursor(mock_data_with_claude):
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)

    # Cursor inicial em (0, 0).
    assert view.cursor_row == 0
    assert view.cursor_col == 0

    # DOWN: row+1
    view.handle_key("DOWN", MagicMock())
    assert view.cursor_row == 1

    # RIGHT: col+1
    view.handle_key("RIGHT", MagicMock())
    assert view.cursor_col == 1

    # UP + LEFT back.
    view.handle_key("UP", MagicMock())
    view.handle_key("LEFT", MagicMock())
    assert view.cursor_row == 0
    assert view.cursor_col == 0


def test_q_returns_to_dashboard(mock_data_with_claude):
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    result = view.handle_key("q", MagicMock())

    # Deve sinalizar nav back para dashboard (ou back/quit). ActionResult
    # não pode ser None — qualquer das três é aceitável para o skeleton.
    from _panel import Action
    assert result is not None
    assert result.kind in (Action.NAV, Action.BACK, Action.QUIT)


# ===========================================================================
# Task 19 — pickers contextuais Worker + Model
# ===========================================================================


def test_worker_picker_options(mock_data_with_claude):
    """Worker picker mostra deile-worker, claude-worker, (global default)."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    options = view._worker_picker_options()
    assert "deile-worker" in options
    assert "claude-worker" in options
    assert any("global" in o.lower() or "default" in o.lower() for o in options)


def test_model_picker_restricted_when_claude_worker(mock_data_with_claude):
    """Worker=claude-worker → model picker só anthropic:* + (default)."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    # Faz `_load_all_models` usar o fallback estático com providers múltiplos.
    options = view._model_picker_options(worker="claude-worker")

    # Toda opção (exceto sentinelas de "default/clear") deve ser anthropic:*.
    for opt in options:
        is_anthropic = opt.startswith("anthropic:")
        is_sentinel = ("default" in opt.lower() or "clear" in opt.lower())
        assert is_anthropic or is_sentinel, \
            f"option {opt!r} não é anthropic-eligible nem sentinela"
    # Pelo menos um anthropic concreto deve existir.
    assert any(o.startswith("anthropic:") for o in options), \
        "picker restrito não tem nenhum anthropic:* concreto"


def test_model_picker_open_when_deile_worker(mock_data_with_claude):
    """Worker=deile-worker → model picker mostra TODOS providers."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    options = view._model_picker_options(worker="deile-worker")

    # Pelo menos 2 providers diferentes devem aparecer (ex: anthropic,
    # openai, deepseek, google) — confirma que não há restrição.
    providers = {opt.split(":", 1)[0] for opt in options if ":" in opt}
    assert len(providers) >= 2, \
        f"só providers {providers} no picker — esperado múltiplos"


def test_enter_on_worker_column_routes_to_worker_picker(mock_data_with_claude):
    """[enter] na coluna 0 (Worker) deve mudar para estado de picker de worker."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view.cursor_row = 0  # primeira stage
    view.cursor_col = 0  # coluna Worker
    result = view.handle_key("\r", MagicMock())

    # Picker abriu — view tem estado modal ativo (mode != None).
    assert view.mode is not None
    assert view.mode[0] == "worker"
    # ActionResult não é NOOP (algo aconteceu).
    from _panel import Action
    assert result.kind != Action.NOOP


def test_enter_on_model_column_routes_to_model_picker(mock_data_with_claude):
    """[enter] na coluna 1 (Model) deve abrir picker de model."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view.cursor_row = 0
    view.cursor_col = 1  # coluna Model
    result = view.handle_key("\r", MagicMock())

    assert view.mode is not None
    assert view.mode[0] == "model"
    from _panel import Action
    assert result.kind != Action.NOOP


def test_enter_on_global_row_opens_global_picker(mock_data_with_claude):
    """[enter] na linha 'Global default' (cursor_row == N) abre picker global."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    # Mover para a linha "Global default" (índice == len(stages)).
    view.cursor_row = 5  # 5 stages → global row at index 5
    view.cursor_col = 0
    result = view.handle_key("\r", MagicMock())

    # Picker abriu — estado modal ativo, e a flag global indica.
    assert view.mode is not None
    assert view.mode[0] in ("global_worker", "global_model")
    from _panel import Action
    assert result.kind != Action.NOOP


def test_esc_in_picker_closes_picker_modal(mock_data_with_claude):
    """ESC dentro do picker fecha o modal sem aplicar nada."""
    from _panel import DispatchMatrixView

    view = DispatchMatrixView(data=mock_data_with_claude)
    view.cursor_row = 0
    view.cursor_col = 0
    view.handle_key("\r", MagicMock())  # abre worker picker
    assert view.mode is not None

    view.handle_key("ESC", MagicMock())  # fecha
    assert view.mode is None
