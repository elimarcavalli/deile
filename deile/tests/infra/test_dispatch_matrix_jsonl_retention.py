"""Testes da retenção JSONL editável no DispatchMatrixView (issue #445 parte 2).

O Humano controla por quanto tempo os transcripts JSONL órfãos sobrevivem
antes de serem colhidos para o ledger + podados. Além da env var no manifest,
a tela [d] do painel expõe o atalho [J] (maiúsculo — 'j' minúsculo é navegação
DOWN) que abre um prompt numérico e aplica em claude-worker + cron via
``kubectl set env`` (sem rebuild).

Cobre:
- [J] abre retention_prompt; 'j' minúsculo continua navegação DOWN.
- _handle_retention_prompt_key: dígitos, multi-dígito, backspace, enter, esc.
- _apply_retention: demo (no-op), sem kubectl (erro), inválido (<1), sucesso
  (seta nos DOIS alvos), value=None (clear → default 30), falha parcial.
- Render: modal retention_prompt aparece no output.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))


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


# --------------------------------------------------------------------------- #
# [J] abre o prompt; 'j' minúsculo NÃO (continua navegação DOWN)
# --------------------------------------------------------------------------- #

def test_J_uppercase_opens_retention_prompt(view_demo, app_stub):
    view_demo.cursor_row = 0
    view_demo.handle_key("J", app_stub)
    assert view_demo.mode is not None
    kind, _, _ = view_demo.mode
    assert kind == "retention_prompt"


def test_j_lowercase_is_navigation_not_retention(view_demo, app_stub):
    """Regressão: 'j' minúsculo é DOWN (vim); só 'J' abre o prompt."""
    view_demo.cursor_row = 0
    view_demo.handle_key("j", app_stub)
    assert view_demo.mode is None          # não abriu modal
    assert view_demo.cursor_row == 1        # navegou para baixo


# --------------------------------------------------------------------------- #
# _handle_retention_prompt_key
# --------------------------------------------------------------------------- #

def test_retention_prompt_digit_input(view_demo, app_stub):
    view_demo._open_retention_prompt()
    view_demo.handle_key("7", app_stub)
    _, _, opts = view_demo.mode
    assert "7" in opts[0]


def test_retention_prompt_multi_digit(view_demo, app_stub):
    view_demo._open_retention_prompt()
    view_demo.handle_key("6", app_stub)
    view_demo.handle_key("0", app_stub)
    _, _, opts = view_demo.mode
    assert opts[0] == "60"


def test_retention_prompt_backspace(view_demo, app_stub):
    view_demo._open_retention_prompt()
    view_demo.handle_key("3", app_stub)
    view_demo.handle_key("0", app_stub)
    view_demo.handle_key("BACKSPACE", app_stub)
    _, _, opts = view_demo.mode
    assert opts[0] == "3"


def test_retention_prompt_esc_cancels(view_demo, app_stub):
    view_demo._open_retention_prompt()
    view_demo.handle_key("ESC", app_stub)
    assert view_demo.mode is None
    assert "cancel" in (view_demo.last_msg or "").lower()


def test_retention_prompt_enter_applies_demo(view_demo, app_stub):
    view_demo._open_retention_prompt()
    view_demo.handle_key("3", app_stub)
    view_demo.handle_key("0", app_stub)
    view_demo.handle_key("\r", app_stub)
    assert view_demo.mode is None
    assert view_demo.last_msg is not None


def test_retention_prompt_enter_empty_applies_clear(view_demo, app_stub):
    view_demo._open_retention_prompt()
    view_demo.handle_key("\r", app_stub)   # buffer vazio → _apply_retention(None)
    assert view_demo.mode is None


# --------------------------------------------------------------------------- #
# _apply_retention
# --------------------------------------------------------------------------- #

def test_apply_retention_demo_mode(view_demo):
    view_demo._apply_retention("30")
    assert view_demo.last_ok is False
    assert "demo" in (view_demo.last_msg or "").lower()


def test_apply_retention_no_kubectl(view_with_data):
    with patch("_panel.kubectl_bin", return_value=None):
        view_with_data._apply_retention("30")
    assert view_with_data.last_ok is False
    assert "kubectl" in (view_with_data.last_msg or "").lower()


def test_apply_retention_invalid_value(view_with_data):
    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"):
        view_with_data._apply_retention("0")   # < 1 → inválido
    assert view_with_data.last_ok is False
    assert "inválido" in (view_with_data.last_msg or "").lower()


def test_apply_retention_success_sets_both_targets(view_with_data):
    """Sucesso aplica nos DOIS alvos (deployment + cronjob) e reporta ok."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        return m

    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run", side_effect=fake_run):
        view_with_data._apply_retention("45")

    assert view_with_data.last_ok is True
    assert "45" in (view_with_data.last_msg or "")
    targets = " ".join(" ".join(c) for c in calls)
    assert "deployment/claude-worker" in targets
    assert "cronjob/claude-worker-cleanup" in targets
    assert "DEILE_CLAUDE_JSONL_RETENTION_DAYS=45" in targets


def test_apply_retention_clear_removes_override(view_with_data):
    def fake_run(cmd, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        return m

    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run", side_effect=fake_run):
        view_with_data._apply_retention(None)

    assert view_with_data.last_ok is True
    assert "default" in (view_with_data.last_msg or "").lower()


def test_apply_retention_cron_notfound_is_skipped_not_failed(view_with_data):
    """CronJob ausente (NotFound) é PULADO — deployment ok → sucesso geral.
    O cron de cleanup nem sempre está aplicado; só o deployment é obrigatório."""
    def fake_run(cmd, **kw):
        m = MagicMock()
        flat = " ".join(cmd)
        if "cronjob" in flat:
            m.returncode = 1
            m.stderr = 'Error from server (NotFound): cronjobs.batch "x" not found'
        else:
            m.returncode = 0
            m.stderr = ""
        return m

    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run", side_effect=fake_run):
        view_with_data._apply_retention("30")

    assert view_with_data.last_ok is True
    assert "claude-worker" in (view_with_data.last_msg or "")
    assert "pulado" in (view_with_data.last_msg or "").lower()


def test_apply_retention_hard_failure_on_deployment(view_with_data):
    """Falha REAL no deployment (não-NotFound) → last_ok False."""
    def fake_run(cmd, **kw):
        m = MagicMock()
        flat = " ".join(cmd)
        if "deployment" in flat:
            m.returncode = 1
            m.stderr = "Error from server (Forbidden): deployments forbidden"
        else:
            m.returncode = 0
            m.stderr = ""
        return m

    with patch("_panel.kubectl_bin", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run", side_effect=fake_run):
        view_with_data._apply_retention("30")

    assert view_with_data.last_ok is False
    assert "parcial" in (view_with_data.last_msg or "").lower()


# --------------------------------------------------------------------------- #
# Render: modal retention_prompt aparece
# --------------------------------------------------------------------------- #

def test_render_retention_prompt_modal_visible(view_demo, app_stub):
    from rich.console import Console
    view_demo.handle_key("J", app_stub)
    console = Console(width=120)
    renderable = view_demo.render(app_stub)
    with console.capture() as cap:
        console.print(renderable)
    out = cap.get()
    assert "RETENÇÃO" in out.upper() or "RETENTION_DAYS" in out.upper()
