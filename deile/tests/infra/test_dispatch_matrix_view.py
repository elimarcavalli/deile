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
