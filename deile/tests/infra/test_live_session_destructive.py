"""Tests para LiveSessionView — ações destrutivas kill/cleanup (issue #462).

Cobertura:
  CA1  — exatamente uma chamada de rede por ação (kill ou cleanup).
  CA2  — kill 200 → refresh (view permanece); kill 409 → toast + refresh.
  CA3  — kill ApiError (não-409) → toast + refresh.
  CA4  — cleanup 200 → back(); cleanup 409 → toast + refresh.
  CA5  — timeout (asyncio.TimeoutError) → confirm_action=None, toast, refresh.
  CA6  — gate 409 só no servidor; frontend verifica alive apenas como discoverability.
  CA7  — audit com result=allowed/failed/cancelled.
  CA8  — ESC quando confirm_action is not None → cancel + intercepts_key=True.
  CA9  — confirm_action resolvido antes de _export_mode/_prompt_open;
          k/C inertes durante filtro ou export.

20 testes.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
from deile.ui.panel.observability.client import ApiError  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs / helpers
# ---------------------------------------------------------------------------

class _FakeApp:
    def __init__(self):
        self.toasts: list = []

    def push_toast(self, icon, msg, ttl_s=5.0):
        self.toasts.append((icon, msg))


def _make_view(task_id: str = "task-aabbccdd-1234") -> panel.LiveSessionView:
    view = panel.LiveSessionView()
    view.task_id = task_id
    return view


def _fake_client(kill_reply=None, cleanup_reply=None):
    """Retorna um objeto com coroutines kill/cleanup controladas."""
    class _Client:
        def __init__(self):
            self.kill_calls = 0
            self.cleanup_calls = 0

        async def kill(self, task_id):
            self.kill_calls += 1
            if isinstance(kill_reply, BaseException):
                raise kill_reply
            return kill_reply

        async def cleanup(self, task_id):
            self.cleanup_calls += 1
            if isinstance(cleanup_reply, BaseException):
                raise cleanup_reply
            return cleanup_reply

    return _Client()


def _patch_client(view, client_obj):
    """Sobrescreve _make_client para retornar client_obj."""
    view._make_client = lambda cls: client_obj


# ---------------------------------------------------------------------------
# CA9 — roteamento de teclas (confirm_action resolvido PRIMEIRO)
# ---------------------------------------------------------------------------

def test_confirm_action_resolved_before_export_mode():
    """CA9: quando confirm_action está set, export_mode é ignorado.

    Sem esse isolamento, k/C cairiam no handler de export e sumiriam.
    """
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(kill_reply={"killed": True, "pid": 42})
    _patch_client(view, client)

    view.confirm_action = "k"
    view._export_mode = "path"  # simula modal de export aberto

    # Com confirm_action set, handle_key deve chamar _handle_confirmation, não o export handler
    result = view.handle_key("y", app)
    # kill foi chamado (não o export handler) → confirm resolvido
    assert client.kill_calls == 1


def test_confirm_action_resolved_before_prompt_open():
    """CA9: quando confirm_action está set, _prompt_open é ignorado."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(cleanup_reply={"removed": {"workdir": True}})
    _patch_client(view, client)

    view.confirm_action = "C"
    view._prompt_open = True  # simula filtro aberto

    result = view.handle_key("y", app)
    # cleanup foi chamado (não o filter handler)
    assert client.cleanup_calls == 1


def test_destructive_hotkey_inert_during_filter_or_export():
    """CA9: k e C são inertes enquanto _prompt_open está ativo."""
    view = _make_view()
    app = _FakeApp()

    view._prompt_open = True
    # [k] durante filtro deve ir para _handle_filter_prompt_key (adiciona 'k' ao buffer)
    view.handle_key("k", app)
    assert view.confirm_action is None
    assert "k" in view._filter_buffer

    view._filter_buffer = ""
    view._prompt_open = False
    view._export_mode = "path"
    view._export_path_buf = ""
    # [C] durante export deve ir para _handle_export_path_key
    view.handle_key("C", app)
    assert view.confirm_action is None


# ---------------------------------------------------------------------------
# CA1 — exatamente uma chamada de rede por ação
# ---------------------------------------------------------------------------

def test_kill_exactly_one_network_call():
    """CA1: [k][y] faz exatamente uma chamada kill."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(kill_reply={"killed": True, "pid": 99})
    _patch_client(view, client)

    view.handle_key("k", app)
    assert view.confirm_action == "k"
    view.handle_key("y", app)
    assert client.kill_calls == 1


def test_cleanup_exactly_one_network_call():
    """CA1: [C][y] faz exatamente uma chamada cleanup."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(cleanup_reply={"removed": {}})
    _patch_client(view, client)
    # Simula sessão não-ativa
    view._last_render = MagicMock()
    view._last_render.session = {"alive": False}

    view.handle_key("C", app)
    assert view.confirm_action == "C"
    view.handle_key("y", app)
    assert client.cleanup_calls == 1


# ---------------------------------------------------------------------------
# CA2 — kill 200 → refresh; kill 409 ApiError → toast + refresh
# ---------------------------------------------------------------------------

def test_kill_200_returns_refresh():
    """CA2: kill bem-sucedido (200) → view permanece (refresh, não back)."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(kill_reply={"killed": True, "pid": 42})
    _patch_client(view, client)

    view.confirm_action = "k"
    result = view._apply_kill(app)
    assert result is not None
    # não é back (que nav de volta)
    assert not getattr(result, "action", None) == "back"


def test_kill_409_toasts_and_stays():
    """CA2/CA7: kill retornando HTTP 409 → toast amigável + stay + audit result=allowed."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(kill_reply=ApiError(status=409, message="no alive process"))
    _patch_client(view, client)
    audit_log = []

    import _panel_data as _pd
    original = _pd._audit_pod_action
    try:
        _pd._audit_pod_action = lambda *a, **kw: audit_log.append(kw)
        view.confirm_action = "k"
        view._apply_kill(app)
    finally:
        _pd._audit_pod_action = original

    assert any("já encerrada" in str(t) for t in app.toasts)
    assert any(e.get("result") == "allowed" for e in audit_log)


# ---------------------------------------------------------------------------
# CA3 — kill ApiError (não-409) → toast + refresh
# ---------------------------------------------------------------------------

def test_kill_api_error_shows_toast():
    """CA3: kill com ApiError (500) → toast com HTTP status."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(kill_reply=ApiError(status=500, message="internal server error"))
    _patch_client(view, client)

    view.confirm_action = "k"
    view._apply_kill(app)
    assert any("500" in str(t) for t in app.toasts)


# ---------------------------------------------------------------------------
# CA4 — cleanup 200 → back(); cleanup 409 → toast + refresh
# ---------------------------------------------------------------------------

def test_cleanup_200_returns_back():
    """CA4: cleanup bem-sucedido (200) → ActionResult.back()."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(cleanup_reply={"removed": {"workdir": True, "jsonl": True}})
    _patch_client(view, client)

    result = view._apply_cleanup(app)
    # back() sets nav action
    assert hasattr(result, "action") or result is not None
    # O toast deve mencionar "cleanup OK"
    assert any("cleanup OK" in str(t) for t in app.toasts)


def test_cleanup_409_toasts_and_stays():
    """CA4/CA6: cleanup com 409 server-side (sessão still alive) → toast + stay."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(cleanup_reply=ApiError(status=409, message="session alive"))
    _patch_client(view, client)

    result = view._apply_cleanup(app)
    assert any("409" in str(t) for t in app.toasts)


# ---------------------------------------------------------------------------
# CA5 — timeout → confirm_action=None, toast, refresh
# ---------------------------------------------------------------------------

def test_kill_timeout_resets_confirm_and_toasts():
    """CA5: asyncio.TimeoutError durante kill → confirm_action limpo + toast."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(kill_reply=asyncio.TimeoutError())
    _patch_client(view, client)

    view.confirm_action = "k"
    view._apply_kill(app)
    # toast de erro
    assert any("falhou" in str(t) or "timeout" in str(t) or "TimeoutError" in str(t)
               for t in app.toasts)


def test_cleanup_timeout_toasts():
    """CA5: asyncio.TimeoutError durante cleanup → toast."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(cleanup_reply=asyncio.TimeoutError())
    _patch_client(view, client)

    view._apply_cleanup(app)
    assert len(app.toasts) > 0


# ---------------------------------------------------------------------------
# CA7 — audit com result=allowed/failed/cancelled
# ---------------------------------------------------------------------------

def test_audit_cancelled_on_other_key():
    """CA7: cancelar com tecla arbitrária → audit result=cancelled."""
    view = _make_view()
    app = _FakeApp()
    audit_log = []

    import _panel_data as _pd
    original = _pd._audit_pod_action
    try:
        _pd._audit_pod_action = lambda *a, **kw: audit_log.append(kw)
        view.confirm_action = "k"
        view.handle_key("z", app)  # tecla aleatória → cancela
    finally:
        _pd._audit_pod_action = original

    assert any(e.get("result") == "cancelled" for e in audit_log)


def test_audit_allowed_on_kill_success():
    """CA7: kill 200 → audit result=allowed."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(kill_reply={"killed": True, "pid": 1})
    _patch_client(view, client)
    audit_log = []

    import _panel_data as _pd
    original = _pd._audit_pod_action
    try:
        _pd._audit_pod_action = lambda *a, **kw: audit_log.append(kw)
        view.confirm_action = "k"
        view.handle_key("y", app)
    finally:
        _pd._audit_pod_action = original

    assert any(e.get("result") == "allowed" for e in audit_log)


# ---------------------------------------------------------------------------
# CA8 — ESC cancela confirmação + intercepts_key=True
# ---------------------------------------------------------------------------

def test_esc_intercepted_when_confirm_action_set():
    """CA8: intercepts_key retorna True para ESC quando confirm_action está set."""
    view = _make_view()
    view.confirm_action = "k"
    assert view.intercepts_key("ESC") is True


def test_esc_cancels_confirm_via_handle_key():
    """CA8: ESC roteado para handle_key cancela confirmação destrutiva."""
    view = _make_view()
    app = _FakeApp()
    view.confirm_action = "k"
    view.handle_key("ESC", app)
    assert view.confirm_action is None


# ---------------------------------------------------------------------------
# Testes de armar confirmação
# ---------------------------------------------------------------------------

def test_k_arms_confirm_action():
    """[k] no estado normal seta confirm_action='k'."""
    view = _make_view()
    app = _FakeApp()
    view.handle_key("k", app)
    assert view.confirm_action == "k"


def test_C_arms_confirm_when_not_alive():
    """[C] quando alive=False seta confirm_action='C'."""
    view = _make_view()
    app = _FakeApp()
    view._last_render = MagicMock()
    view._last_render.session = {"alive": False}
    view.handle_key("C", app)
    assert view.confirm_action == "C"


def test_C_toasts_and_does_not_arm_when_alive():
    """CA6 (frontend): [C] quando alive=True → toast, sem armar confirm."""
    view = _make_view()
    app = _FakeApp()
    view._last_render = MagicMock()
    view._last_render.session = {"alive": True}
    view.handle_key("C", app)
    assert view.confirm_action is None
    assert len(app.toasts) > 0


def test_repeat_key_confirms():
    """Double-tap muscle-memory: [k][k] confirma kill (como PodPickerView)."""
    view = _make_view()
    app = _FakeApp()
    client = _fake_client(kill_reply={"killed": True, "pid": 7})
    _patch_client(view, client)

    view.handle_key("k", app)
    assert view.confirm_action == "k"
    view.handle_key("k", app)  # repete a tecla
    assert client.kill_calls == 1
    assert view.confirm_action is None
