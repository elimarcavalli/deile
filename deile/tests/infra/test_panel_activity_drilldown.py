"""Tests for issue #446: ACTIVITY widget drill-down.

Coverage:
  1.  ActivityEvent.source_pod defaults to "" (retrocompatible).
  2.  ActivityEvent.task_id defaults to None (retrocompatible).
  3.  ActivityEvent source_pod and task_id can be set at construction time.
  4.  DashboardView initial state: _activity_focused=False, _activity_cursor=0.
  5.  Key [a] enters activity cursor mode, cursor resets to 0.
  6.  Key [esc] exits cursor mode.
  7.  Up arrow wraps cursor from 0 to last row.
  8.  Down arrow advances cursor.
  9.  [k] moves up, [j] moves down.
  10. [enter] on claude-worker row with task_id -> nav("live-session").
  11. [enter] on non-claude-worker row with source_pod -> nav("pod-watch").
  12. [enter] on row without source_pod -> refresh (no nav).
  13. [enter] with empty event list -> refresh.
  14. intercepts_key returns True for ESC/arrows/jk/enter when focused.
  15. intercepts_key returns False for navigation keys when focused.
  16. intercepts_key returns False for ESC when not focused.
  17. LiveSessionView is registered under "live-session" in _build_views().
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

_UTC = timezone.utc


def _ts() -> datetime:
    return datetime(2026, 1, 1, 16, 0, 0, tzinfo=_UTC)


def _event(actor: str = "pipeline", source_pod: str = "",
           task_id=None) -> pd.ActivityEvent:
    return pd.ActivityEvent(
        ts=_ts(), actor=actor, action="dispatch",
        target="#1", detail="", source_pod=source_pod, task_id=task_id,
    )


def _mock_app() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# 1-3: ActivityEvent new fields
# ---------------------------------------------------------------------------

class TestActivityEventNewFields:
    def test_source_pod_default(self):
        ev = pd.ActivityEvent(ts=_ts(), actor="p", action="a", target="t", detail="d")
        assert ev.source_pod == ""

    def test_task_id_default(self):
        ev = pd.ActivityEvent(ts=_ts(), actor="p", action="a", target="t", detail="d")
        assert ev.task_id is None

    def test_fields_settable(self):
        ev = pd.ActivityEvent(
            ts=_ts(), actor="claude-worker", action="dispatch",
            target="#5", detail="x", source_pod="claude-worker-abc-xyz",
            task_id="task-123",
        )
        assert ev.source_pod == "claude-worker-abc-xyz"
        assert ev.task_id == "task-123"


# ---------------------------------------------------------------------------
# 4: Initial state
# ---------------------------------------------------------------------------

class TestDashboardViewInitialState:
    def test_activity_focused_false(self):
        v = panel.DashboardView()
        assert v._activity_focused is False

    def test_activity_cursor_zero(self):
        v = panel.DashboardView()
        assert v._activity_cursor == 0


# ---------------------------------------------------------------------------
# 5-6: Entering / exiting focus mode
# ---------------------------------------------------------------------------

class TestDashboardViewFocusToggle:
    def test_a_key_enters_focus(self):
        v = panel.DashboardView()
        result = v.handle_key("a", _mock_app())
        assert v._activity_focused is True
        assert v._activity_cursor == 0
        assert result.kind == panel.Action.REFRESH

    def test_escape_exits_focus(self):
        # KeyReader emite o token "ESC" para o escape (ver KeyReader.read).
        v = panel.DashboardView()
        v._activity_focused = True
        v._last_activity_events = [_event()]
        result = v.handle_key("ESC", _mock_app())
        assert v._activity_focused is False
        assert result.kind == panel.Action.REFRESH

    def test_escape_raw_token_also_exits_focus(self):
        # Retrocompat: o token raw "\x1b" continua aceito.
        v = panel.DashboardView()
        v._activity_focused = True
        v._last_activity_events = [_event()]
        result = v.handle_key("\x1b", _mock_app())
        assert v._activity_focused is False
        assert result.kind == panel.Action.REFRESH


# ---------------------------------------------------------------------------
# 7-9: Cursor navigation
# ---------------------------------------------------------------------------

class TestDashboardViewCursorNavigation:
    def _view_with_events(self, n: int = 3) -> panel.DashboardView:
        v = panel.DashboardView()
        v._activity_focused = True
        v._activity_cursor = 0
        v._last_activity_events = [_event() for _ in range(n)]
        return v

    def test_up_arrow_wraps(self):
        # KeyReader emite "UP"/"DOWN" para as setas (ver KeyReader._ARROW).
        v = self._view_with_events(3)
        v.handle_key("UP", _mock_app())
        assert v._activity_cursor == 2  # 0 - 1 mod 3

    def test_down_arrow_advances(self):
        v = self._view_with_events(3)
        v.handle_key("DOWN", _mock_app())
        assert v._activity_cursor == 1

    def test_up_arrow_raw_token_also_wraps(self):
        v = self._view_with_events(3)
        v.handle_key("\x1b[A", _mock_app())
        assert v._activity_cursor == 2

    def test_down_arrow_raw_token_also_advances(self):
        v = self._view_with_events(3)
        v.handle_key("\x1b[B", _mock_app())
        assert v._activity_cursor == 1

    def test_k_moves_up(self):
        v = self._view_with_events(3)
        v._activity_cursor = 2
        v.handle_key("k", _mock_app())
        assert v._activity_cursor == 1

    def test_j_moves_down(self):
        v = self._view_with_events(3)
        v._activity_cursor = 1
        v.handle_key("j", _mock_app())
        assert v._activity_cursor == 2


# ---------------------------------------------------------------------------
# 10-13: Drill-down dispatch on [enter]
# ---------------------------------------------------------------------------

class TestDashboardViewDrillDown:
    def _view_with(self, ev: pd.ActivityEvent) -> panel.DashboardView:
        v = panel.DashboardView()
        v._activity_focused = True
        v._activity_cursor = 0
        v._last_activity_events = [ev]
        return v

    def test_enter_claude_worker_with_task_id_navs_live_session(self):
        ev = _event(actor="claude-worker", source_pod="claude-worker-xyz", task_id="task-abc")
        v = self._view_with(ev)
        result = v.handle_key("\r", _mock_app())
        assert result.kind == panel.Action.NAV
        assert result.target == "live-session"
        assert result.payload["task_id"] == "task-abc"
        assert result.payload["pod_name"] == "claude-worker-xyz"

    def test_enter_non_claude_worker_with_pod_navs_pod_watch(self):
        ev = _event(actor="deile-worker", source_pod="deile-worker-abc-1")
        v = self._view_with(ev)
        result = v.handle_key("\r", _mock_app())
        assert result.kind == panel.Action.NAV
        assert result.target == "pod-watch"
        assert result.payload["pod_name"] == "deile-worker-abc-1"
        assert result.payload["pod_role"] == "deile-worker"

    def test_enter_claude_worker_without_task_id_falls_back_to_pod_watch(self):
        ev = _event(actor="claude-worker", source_pod="claude-worker-xyz", task_id=None)
        v = self._view_with(ev)
        result = v.handle_key("\r", _mock_app())
        # No task_id -> fallback to pod-watch since source_pod is set
        assert result.kind == panel.Action.NAV
        assert result.target == "pod-watch"

    def test_enter_no_pod_refreshes(self):
        ev = _event(actor="pipeline", source_pod="")
        v = self._view_with(ev)
        result = v.handle_key("\r", _mock_app())
        assert result.kind == panel.Action.REFRESH

    def test_enter_empty_events_refreshes(self):
        v = panel.DashboardView()
        v._activity_focused = True
        v._activity_cursor = 0
        v._last_activity_events = []
        result = v.handle_key("\r", _mock_app())
        assert result.kind == panel.Action.REFRESH

    def test_enter_newline_also_works(self):
        ev = _event(actor="claude-worker", source_pod="pod-x", task_id="t-1")
        v = self._view_with(ev)
        result = v.handle_key("\n", _mock_app())
        assert result.kind == panel.Action.NAV
        assert result.target == "live-session"


# ---------------------------------------------------------------------------
# 14-16: intercepts_key
# ---------------------------------------------------------------------------

class TestDashboardViewInterceptsKey:
    def test_intercepts_esc_when_focused(self):
        # Token de produção do KeyReader é "ESC" — interceptar é o que evita
        # o focus-trap (sem isto o ESC cai no _handle_global → pop() no-op).
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("ESC") is True

    def test_intercepts_esc_raw_token_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("\x1b") is True

    def test_intercepts_up_arrow_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("UP") is True
        assert v.intercepts_key("\x1b[A") is True

    def test_intercepts_down_arrow_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("DOWN") is True
        assert v.intercepts_key("\x1b[B") is True

    def test_intercepts_enter_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("\r") is True
        assert v.intercepts_key("\n") is True

    def test_intercepts_j_k_when_focused(self):
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("j") is True
        assert v.intercepts_key("k") is True

    def test_does_not_intercept_r_when_focused(self):
        # [r] não é da Activity — não pode ser interceptado (segue global/view).
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("r") is False

    def test_does_not_intercept_q_when_focused(self):
        # [q] sempre encerra o painel (global), mesmo dentro do modo focused.
        v = panel.DashboardView()
        v._activity_focused = True
        assert v.intercepts_key("q") is False

    def test_does_not_intercept_esc_when_not_focused(self):
        v = panel.DashboardView()
        v._activity_focused = False
        assert v.intercepts_key("ESC") is False
        assert v.intercepts_key("\x1b") is False


# ---------------------------------------------------------------------------
# 17: LiveSessionView registered
# ---------------------------------------------------------------------------

class TestLiveSessionViewRegistered:
    def test_live_session_in_build_views(self):
        views = panel._build_views()
        assert "live-session" in views
        assert isinstance(views["live-session"], panel.LiveSessionView)


# ---------------------------------------------------------------------------
# Regressão: [A] reabilita a ActionsView (que ficou órfã ao [a] ser
# repurposed para a Activity em #436/#446).
# ---------------------------------------------------------------------------

class TestActionsViewHotkey:
    def test_uppercase_A_navs_to_actions(self):
        v = panel.DashboardView()
        result = v.handle_key("A", _mock_app())
        assert result.kind == panel.Action.NAV
        assert result.target == "actions"

    def test_uppercase_A_does_not_enter_activity_focus(self):
        # [A] é Actions, NÃO entra no modo cursor da Activity.
        v = panel.DashboardView()
        v.handle_key("A", _mock_app())
        assert v._activity_focused is False

    def test_lowercase_a_enters_activity_not_actions(self):
        # [a] minúsculo continua sendo a Activity (não navega para actions).
        v = panel.DashboardView()
        result = v.handle_key("a", _mock_app())
        assert v._activity_focused is True
        assert result.kind == panel.Action.REFRESH

    def test_actions_view_registered(self):
        views = panel._build_views()
        assert "actions" in views
        assert isinstance(views["actions"], panel.ActionsView)

    def test_actions_view_renders_without_data(self):
        # A view não pode crashar sem cluster/dados (k8s offline).
        view = panel.ActionsView(data=None)
        app = panel.PanelApp(views={"actions": view}, root="actions")
        out = view.render(app)
        assert out is not None


# ---------------------------------------------------------------------------
# Regressão: ciclo de foco da Activity não pode prender teclas globais.
# Com o token correto do KeyReader ("ESC"/"UP"/"DOWN"), entrar→mover→sair
# devolve o controle global.
# ---------------------------------------------------------------------------

class TestActivityFocusCycle:
    def test_enter_move_esc_releases_global(self):
        v = panel.DashboardView()
        v._last_activity_events = [_event() for _ in range(3)]
        # entra no modo cursor
        v.handle_key("a", _mock_app())
        assert v._activity_focused is True
        # move com setas (tokens de produção)
        v.handle_key("DOWN", _mock_app())
        assert v._activity_cursor == 1
        v.handle_key("UP", _mock_app())
        assert v._activity_cursor == 0
        # ESC sai do modo focused
        v.handle_key("ESC", _mock_app())
        assert v._activity_focused is False
        # após ESC, as teclas globais voltam a navegar
        result = v.handle_key("1", _mock_app())
        assert result.kind == panel.Action.NAV
        assert result.target == "pod-picker"

    def test_global_nav_swallowed_while_focused(self):
        # Enquanto focado, [1] não navega — é NOOP (precisa ESC antes).
        v = panel.DashboardView()
        v._activity_focused = True
        v._last_activity_events = [_event()]
        result = v.handle_key("1", _mock_app())
        assert result.kind == panel.Action.NOOP
        assert v._activity_focused is True

    def test_enter_exit_idempotent(self):
        v = panel.DashboardView()
        v._last_activity_events = [_event()]
        v.handle_key("a", _mock_app())
        v.handle_key("ESC", _mock_app())
        v.handle_key("a", _mock_app())
        assert v._activity_focused is True
        assert v._activity_cursor == 0
        v.handle_key("ESC", _mock_app())
        assert v._activity_focused is False

    def test_esc_with_empty_feed_exits(self):
        # Feed vazio não pode prender o ESC.
        v = panel.DashboardView()
        v._activity_focused = True
        v._last_activity_events = []
        result = v.handle_key("ESC", _mock_app())
        assert v._activity_focused is False
        assert result.kind == panel.Action.REFRESH

    def test_arrows_with_empty_feed_do_not_crash(self):
        v = panel.DashboardView()
        v._activity_focused = True
        v._last_activity_events = []
        v.handle_key("DOWN", _mock_app())
        v.handle_key("UP", _mock_app())
        # cursor permanece em 0 (len_rows=1, % 1 == 0)
        assert v._activity_cursor == 0

    def test_enter_canonical_token_drilldown(self):
        ev = _event(actor="claude-worker", source_pod="pod-x", task_id="t-1")
        v = panel.DashboardView()
        v._activity_focused = True
        v._activity_cursor = 0
        v._last_activity_events = [ev]
        result = v.handle_key("\r", _mock_app())
        assert result.kind == panel.Action.NAV
        assert result.target == "live-session"


# ---------------------------------------------------------------------------
# Render: cursor obsoleto é clampado à janela corrente do feed.
# ---------------------------------------------------------------------------

class TestActivityCursorClamp:
    def test_render_clamps_stale_cursor(self):
        # cursor obsoleto (999) deve ser clampado à janela de linhas visível
        # após o render — nunca apontar para fora dos limites.
        v = panel.DashboardView()
        v._activity_focused = True
        v._activity_cursor = 999
        v._activity_panel()
        n_rows = len(panel._activity_from_data(None, limit=10))
        assert n_rows > 0
        assert v._activity_cursor == n_rows - 1

    def test_render_does_not_crash_and_keeps_cursor_valid(self):
        v = panel.DashboardView()
        v._activity_focused = True
        v._activity_cursor = 0
        out = v._activity_panel()
        assert out is not None
        assert v._activity_cursor >= 0


# ---------------------------------------------------------------------------
# Footer HOTKEYS reflete [a]ctivity + [A]ctions e o modo focused.
# ---------------------------------------------------------------------------

class TestActivityHotkeysFooter:
    def test_normal_footer_lists_activity_and_actions(self):
        v = panel.DashboardView()
        v._activity_focused = False
        hk = v.HOTKEYS
        assert "[a]ctivity" in hk
        assert "[A]ctions" in hk

    def test_focused_footer_shows_cursor_keys_and_esc(self):
        v = panel.DashboardView()
        v._activity_focused = True
        hk = v.HOTKEYS
        assert "esc" in hk.lower()
        assert "ACTIVITY" in hk


# ---------------------------------------------------------------------------
# End-to-end via PanelApp: o despacho real (intercepts_key + _handle_global
# + handle_key) prova que o focus-trap sumiu com os tokens corretos.
# ---------------------------------------------------------------------------

class TestPanelAppActivityDispatch:
    def _app(self) -> panel.PanelApp:
        dash = panel.DashboardView()
        dash._last_activity_events = [_event() for _ in range(3)]
        return panel.PanelApp(views={"dashboard": dash}, root="dashboard")

    def _dispatch(self, app: panel.PanelApp, key: str) -> None:
        """Espelha o caminho de tecla do _run_loop (linhas ~9243-9255)."""
        view = app.current_view
        if view.intercepts_key(key):
            app._apply(view.handle_key(key, app))
        elif app._handle_global(key):
            return
        else:
            app._apply(view.handle_key(key, app))

    def test_esc_unfocuses_instead_of_global_pop(self):
        app = self._app()
        dash = app.current_view
        # entra no modo focused
        self._dispatch(app, "a")
        assert dash._activity_focused is True
        # ESC é interceptado e sai do modo (não dispara pop global no root)
        self._dispatch(app, "ESC")
        assert dash._activity_focused is False
        # o painel continua vivo (não saiu nem empilhou nada)
        assert app.running is True
        assert len(app.stack) == 1

    def test_global_keys_work_after_esc(self):
        app = self._app()
        self._dispatch(app, "a")
        self._dispatch(app, "ESC")
        # [1] agora navega para pod-picker (empilha a view)
        app.views["pod-picker"] = panel.PodPickerView(data=None)
        self._dispatch(app, "1")
        assert app.current_view.name == "pod-picker"

    def test_uppercase_A_pushes_actions_view(self):
        app = self._app()
        app.views["actions"] = panel.ActionsView(data=None)
        self._dispatch(app, "A")
        assert app.current_view.name == "actions"
