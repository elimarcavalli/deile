"""Regressão da MonitorView do painel (`[M]` no deploy.py panel).

Bug (01/jun): entrar na tela `[M]` estourava ``AttributeError: 'int' object
has no attribute 'replace'``. O state do monitor grava ``last_tick`` como
CONTADOR (int) + ``last_tick_epoch`` (epoch), mas o painel tratava
``last_tick`` como timestamp ISO (``_parse_iso(st.last_tick)`` e
``st.last_tick[:19]``). Estes testes reproduzem o render com ``last_tick``
inteiro e garantem que não estoura.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from rich.console import Console

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"


@pytest.fixture
def pm():
    if str(_INFRA_K8S) not in sys.path:
        sys.path.insert(0, str(_INFRA_K8S))
    spec = importlib.util.spec_from_file_location(
        "panel_monitor_test", str(_INFRA_K8S / "_panel_monitor.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["panel_monitor_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _render(renderable) -> str:
    console = Console(file=None, width=120)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def _snapshot_with_int_last_tick(pm):
    return pm.MonitorSnapshot(
        state=pm.MonitorStateData(
            last_tick=49,                 # CONTADOR (int) — era tratado como ISO
            last_tick_epoch=1780344283.0,
            notifications_this_hour=1,
        ),
    )


def test_pod_header_renders_with_int_last_tick(pm):
    view = pm.MonitorView()
    snap = _snapshot_with_int_last_tick(pm)
    out = _render(view._render_pod_header(snap))   # continha _parse_iso(last_tick)
    assert out  # renderizou sem AttributeError


def test_last_tick_panel_renders_with_int(pm):
    view = pm.MonitorView()
    snap = _snapshot_with_int_last_tick(pm)
    out = _render(view._render_last_tick(snap))    # continha last_tick[:19]
    assert "#49" in out                             # mostra o contador


def test_footer_visible_when_body_overflows(pm, monkeypatch):
    """O rodapé de atalhos fica PINADO via Layout — não some quando o corpo
    (ex.: audit log grande) passa da altura do terminal (Live screen=True)."""
    import types

    import _panel
    from rich.panel import Panel
    from rich.text import Text
    # head depende de muitos atributos do app real; aqui só nos importa o
    # footer pinado, então simplificamos o head.
    monkeypatch.setattr(_panel, "_head_panel", lambda title, app: Panel(Text("HEAD")))

    snap = pm.MonitorSnapshot(
        pod=pm.MonitorPodInfo(
            found=True, name="deile-monitor-x", status="Running",
            ready=True, age_s=120,
        ),
        state=pm.MonitorStateData(last_tick=51, last_tick_epoch=1780344283.0),
        audit_tail=[f"2026-06-01T20:00:{i:02d}Z linha de audit {i}"
                    for i in range(40)],   # corpo alto → transborda
    )
    view = pm.MonitorView(
        monitor_provider=types.SimpleNamespace(
            get=lambda: snap, invalidate=lambda: None))

    layout = view._render_safe(object())
    # Renderiza num terminal BAIXO (24 linhas) — o corpo não cabe, mas o
    # footer pinado tem de aparecer mesmo assim.
    console = Console(width=140, height=24)
    with console.capture() as cap:
        console.print(layout)
    out = cap.get()
    assert "[i]interval" in out or "interval" in out
