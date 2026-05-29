"""Tests for the ``OVERRIDES ATIVOS`` banner in DispatchMatrixView.

O banner aparece acima da matriz quando QUALQUER per-stage override está
ativo (worker per-stage, model, timeout_s, max_retries, cost_cap_usd) e
destaca ``retries=0`` em vermelho — o caso que historicamente bloqueou o
pipeline (env ghost ``DEILE_PIPELINE_RETRIES_IMPLEMENT=0``).
"""

import os
import sys
import types
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock

import pytest
from rich.console import Console

_INFRA_K8S = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "infra", "k8s"
)
if _INFRA_K8S not in sys.path:
    sys.path.insert(0, _INFRA_K8S)


@dataclass(frozen=True)
class _Entry:
    stage: str
    worker: str = "deile-worker"
    model: Optional[str] = None
    source: str = "default"
    timeout_s: Optional[int] = None
    max_retries: Optional[int] = None
    cost_cap_usd: Optional[str] = None


@pytest.fixture(autouse=True)
def _stub_panel_data(monkeypatch):
    if "_panel_data" not in sys.modules:
        stub = types.ModuleType("_panel_data")
        stub.NS = "deile"
        stub.kubectl_bin = lambda: None
        stub.BackgroundRefresher = MagicMock
        stub.PanelData = MagicMock
        stub._fmt_age = lambda *a, **kw: ""
        stub._fmt_cpu_display = lambda *a, **kw: ""
        stub._fmt_mem_display = lambda *a, **kw: ""
        stub._pct = lambda *a, **kw: None
        stub.EndpointInfo = MagicMock
        stub.set_stage_cost_cap_usd = MagicMock(return_value=(True, "ok"))
        stub.reset_stage_cost_cap_usd = MagicMock(return_value=(True, "ok"))
        stub._audit_dispatch_mode_change = MagicMock()
        stub._audit_security_policy_change = MagicMock()
        stub.clear_pipeline_dispatch_mode = MagicMock(return_value=(True, "ok"))
        stub.clear_stage_model = MagicMock(return_value=(True, "ok"))
        stub.set_pipeline_dispatch_mode = MagicMock(return_value=(True, "ok"))
        stub.set_pipeline_dispatch_stage = MagicMock(return_value=(True, "ok"))
        stub.set_preferred_model = MagicMock(return_value=(True, "ok"))
        stub.set_stage_model = MagicMock(return_value=(True, "ok"))
        stub.set_stage_timeout = MagicMock(return_value=(True, "ok"))
        stub.set_stage_retries = MagicMock(return_value=(True, "ok"))
        stub.StageDispatchEntry = _Entry
        stub.ClaudeWorkerStatus = MagicMock
        sys.modules["_panel_data"] = stub


def _render(entries):
    """Render the banner and return text + rendered ANSI for assertions."""
    import _panel as panel_mod
    view = panel_mod.DispatchMatrixView(data=None)
    out = view._render_overrides_banner(entries)
    if out is None:
        return None, ""
    console = Console(width=120, record=True)
    console.print(out)
    return out, console.export_text()


def test_banner_hidden_when_no_overrides():
    entries = [
        _Entry("classify"),
        _Entry("implement"),
    ]
    out, _ = _render(entries)
    assert out is None


def test_banner_visible_when_retries_zero():
    entries = [
        _Entry("classify"),
        _Entry("implement", max_retries=0),
    ]
    out, text = _render(entries)
    assert out is not None
    assert "implement" in text
    assert "FAIL-FAST" in text


def test_banner_visible_when_model_overridden():
    entries = [
        _Entry("classify", model="anthropic:claude-opus-4-7"),
    ]
    out, text = _render(entries)
    assert out is not None
    assert "classify" in text
    assert "model=anthropic:claude-opus-4-7" in text


def test_banner_visible_when_timeout_overridden():
    entries = [
        _Entry("implement", timeout_s=900),
    ]
    out, text = _render(entries)
    assert out is not None
    assert "timeout=900s" in text


def test_banner_visible_when_cost_cap_set():
    entries = [
        _Entry("implement", cost_cap_usd="5.00"),
    ]
    out, text = _render(entries)
    assert out is not None
    assert "cap=$5.00" in text


def test_banner_visible_when_worker_overridden_per_stage():
    """Worker per-stage (source=env) aparece no banner; source=global não."""
    entries = [
        _Entry("classify", worker="claude-worker", source="env"),
        _Entry("implement", worker="claude-worker", source="global"),
    ]
    out, text = _render(entries)
    assert out is not None
    assert "classify" in text
    assert "worker=claude-worker" in text
    # implement não tem outros overrides nem worker per-stage — não aparece
    assert "implement" not in text


def test_banner_lists_multiple_overrides_per_stage():
    entries = [
        _Entry("implement",
               model="openai:gpt-4-turbo",
               timeout_s=900,
               max_retries=5,
               cost_cap_usd="10.00"),
    ]
    out, text = _render(entries)
    assert out is not None
    assert "model=openai:gpt-4-turbo" in text
    assert "timeout=900s" in text
    assert "retries=5" in text
    assert "cap=$10.00" in text


def test_banner_does_not_flag_positive_retries_as_fail_fast():
    entries = [
        _Entry("implement", max_retries=3),
    ]
    out, text = _render(entries)
    assert out is not None
    assert "retries=3" in text
    assert "FAIL-FAST" not in text
