"""Frente 5 — agrupamento de pods por CLASSE de worker na tela [1].

O agrupamento que existia só para ``claude-worker`` foi generalizado para
TODOS os tipos de worker (``deile-worker``, ``claude-worker`` e os da frota
CLI ``<kind>-worker``), reutilizando os helpers de módulo
(``_is_worker_class_role`` / ``_render_grouped_pods`` / ``_role_for_app``) —
sem duplicar a regra entre Dashboard e PodPicker.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402


def _row(name: str, role: str, **extra) -> "panel.PodRow":
    return panel.PodRow(
        icon=extra.get("icon", "●"),
        name=name, role=role,
        status=extra.get("status", "Running"),
        age=extra.get("age", "5m"),
        restarts=extra.get("restarts", "0"),
        last_activity=extra.get("last_activity", "—"),
        doing_now=extra.get("doing_now", "idle"),
        busy=extra.get("busy", False),
    )


# --------------------------------------------------------------------------- #
# _is_worker_class_role
# --------------------------------------------------------------------------- #

def test_worker_class_role_covers_core_and_fleet():
    assert panel._is_worker_class_role("worker")
    assert panel._is_worker_class_role("claude-worker")
    assert panel._is_worker_class_role("codex-worker")
    assert panel._is_worker_class_role("opencode-worker")
    # Infra não é worker-class.
    assert not panel._is_worker_class_role("pipeline")
    assert not panel._is_worker_class_role("monitor")
    assert not panel._is_worker_class_role("bot")
    assert not panel._is_worker_class_role("shell")
    assert not panel._is_worker_class_role("other")


def test_ordered_worker_roles_core_first_then_alpha():
    roles = {"qwen-worker", "claude-worker", "worker", "codex-worker"}
    ordered = panel._ordered_worker_roles(roles)
    assert ordered[:2] == ["worker", "claude-worker"]
    assert ordered[2:] == ["codex-worker", "qwen-worker"]  # alfabético


# --------------------------------------------------------------------------- #
# _role_for_app — frota CLI derivada do registro
# --------------------------------------------------------------------------- #

def test_role_for_app_static_and_fleet():
    assert pd._role_for_app("deile-worker") == "worker"
    assert pd._role_for_app("claude-worker") == "claude-worker"
    assert pd._role_for_app("deile-pipeline") == "pipeline"
    # Frota CLI: app == role == "<kind>-worker" (derivado do registro).
    fleet = pd._cli_fleet_worker_apps()
    assert "codex-worker" in fleet
    assert pd._role_for_app("codex-worker") == "codex-worker"
    # App desconhecido → other.
    assert pd._role_for_app("random-thing") == "other"


# --------------------------------------------------------------------------- #
# _render_grouped_pods — um cabeçalho por tipo de worker
# --------------------------------------------------------------------------- #

def _render_to_text(rows) -> str:
    tbl = Table(expand=True)
    for col in ("icon", "pod", "status", "age", "r", "last", "doing"):
        tbl.add_column(col)
    panel._render_grouped_pods(
        tbl, rows, panel._restart_text, panel._doing_now_render,
    )
    console = Console(width=200)
    with console.capture() as cap:
        console.print(tbl)
    return cap.get()


def test_render_groups_every_worker_type():
    rows = [
        _row("deile-pipeline-0", "pipeline"),
        _row("deile-worker-aaa", "worker"),
        _row("deile-worker-bbb", "worker"),
        _row("claude-worker-ccc", "claude-worker", busy=True),
        _row("codex-worker-ddd", "codex-worker"),
        _row("codex-worker-eee", "codex-worker", status="NotReady"),
    ]
    out = _render_to_text(rows)
    # Cada tipo de worker tem um cabeçalho de grupo com contagem.
    assert "deile-worker (2 réplica(s) · 2 ready" in out
    assert "claude-worker (1 réplica(s) · 1 ready · 1 ativa(s))" in out
    assert "codex-worker (2 réplica(s) · 1 ready" in out
    # O pod de infra NÃO entra num grupo (renderiza flat antes).
    assert "deile-pipeline-0" in out


def test_render_no_group_header_when_no_workers():
    rows = [
        _row("deile-pipeline-0", "pipeline"),
        _row("deilebot-0", "bot"),
    ]
    out = _render_to_text(rows)
    assert "réplica(s)" not in out  # nenhum cabeçalho de grupo


# --------------------------------------------------------------------------- #
# PodPickerView._rows — agrupa contíguo sem desalinhar cursor
# --------------------------------------------------------------------------- #

def test_pod_picker_rows_groups_workers_contiguously(monkeypatch):
    pods = [
        _row("deile-pipeline-0", "pipeline"),
        _row("codex-worker-a", "codex-worker"),
        _row("deile-worker-b", "worker"),
        _row("claude-worker-c", "claude-worker"),
        _row("codex-worker-d", "codex-worker"),
    ]
    monkeypatch.setattr(panel, "_pod_rows", lambda data: pods)
    monkeypatch.setattr(panel, "_local_process_rows", lambda data: [])
    view = panel.PodPickerView()
    view.data = object()
    out = [r.role for r in view._rows()]
    # Infra primeiro; depois worker / claude-worker / codex-worker contíguos.
    assert out[0] == "pipeline"
    assert out[1] == "worker"
    assert out[2] == "claude-worker"
    assert out[3:] == ["codex-worker", "codex-worker"]


def test_deployment_for_role_maps_fleet_workers():
    assert panel.PodPickerView._deployment_for_role("worker") == "deile-worker"
    assert panel.PodPickerView._deployment_for_role(
        "claude-worker") == "claude-worker"
    assert panel.PodPickerView._deployment_for_role(
        "codex-worker") == "codex-worker"
    assert panel.PodPickerView._deployment_for_role("local-shell") is None
