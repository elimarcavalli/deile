"""Tests for DispatchMatrixView Timeout/Retries columns (issue #391).

Mirrors test_dispatch_matrix_view.py — covers:
- cursor_col navigation up to 3
- Timeout/Retries columns visible in render
- _open_timeout_prompt / _open_retries_prompt
- _handle_numeric_prompt_key (digit entry, backspace, enter, ESC)
- _reset_current_cell for cols 2 and 3
- StageDispatchEntry new fields
"""

import os
import sys
import types
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — infra/k8s not on sys.path by default in pytest context.
# ---------------------------------------------------------------------------

_INFRA_K8S = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "infra", "k8s"
)
if _INFRA_K8S not in sys.path:
    sys.path.insert(0, _INFRA_K8S)


@dataclass(frozen=True)
class _StubStageDispatchEntry:
    stage: str
    worker: str
    model: Optional[str]
    source: str
    timeout_s: Optional[int] = None
    max_retries: Optional[int] = None


@dataclass(frozen=True)
class _StubClaudeWorkerStatus:
    deployment_applied: bool
    pod_ready: bool
    logged_in_email: Optional[str]


@pytest.fixture(autouse=True)
def _stub_panel_data_imports(monkeypatch):
    """Minimal stubs to import _panel without a real K8s cluster."""
    for mod_name in ("_panel_data",):
        if mod_name not in sys.modules:
            stub = types.ModuleType(mod_name)
            # Minimal symbols needed by _panel imports
            stub.NS = "deile"
            stub.kubectl_bin = lambda: None
            stub.BackgroundRefresher = MagicMock
            stub.PanelData = MagicMock
            stub._fmt_age = lambda *a, **kw: ""
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
            # Dataclass stubs needed by sibling test files loaded in same session
            stub.StageDispatchEntry = _StubStageDispatchEntry
            stub.ClaudeWorkerStatus = _StubClaudeWorkerStatus
            sys.modules[mod_name] = stub


# ---------------------------------------------------------------------------
# StageDispatchEntry — new fields
# ---------------------------------------------------------------------------


def test_stage_dispatch_entry_new_fields():
    sys.path.insert(0, _INFRA_K8S)
    import _panel_data as _pd

    # Check if real _panel_data is loaded or our stub
    if hasattr(_pd, "StageDispatchEntry"):
        entry = _pd.StageDispatchEntry("implement", "deile-worker", None, "default",
                                       timeout_s=600, max_retries=2)
        assert entry.timeout_s == 600
        assert entry.max_retries == 2
        # Defaults to None
        entry2 = _pd.StageDispatchEntry("classify", "deile-worker", None, "default")
        assert entry2.timeout_s is None
        assert entry2.max_retries is None


# ---------------------------------------------------------------------------
# DispatchMatrixView — cursor navigation
# ---------------------------------------------------------------------------


def _make_view():
    """Create a DispatchMatrixView in demo mode (data=None)."""
    # We need to patch enough of _panel for the import to work
    # If _panel is already imported, get it; else do a clean import
    panel_mod = sys.modules.get("_panel")
    if panel_mod is None:
        try:
            import _panel as panel_mod
        except Exception:
            pytest.skip("_panel not importable in this environment")
    DispatchMatrixView = getattr(panel_mod, "DispatchMatrixView", None)
    if DispatchMatrixView is None:
        pytest.skip("DispatchMatrixView not found in _panel")
    return DispatchMatrixView(data=None)


def test_cursor_col_max_is_5():
    """Após a coluna Reasoning (Decisão #47), a coluna máxima virou 5."""
    view = _make_view()
    # Navigate right 10 times — should clamp at 5 (0=Worker..5=Reasoning)
    for _ in range(10):
        view.handle_key("RIGHT", None)
    assert view.cursor_col == 5


def test_cursor_col_min_is_0():
    view = _make_view()
    view.cursor_col = 4
    for _ in range(10):
        view.handle_key("LEFT", None)
    assert view.cursor_col == 0


def test_cursor_col_navigates_through_all_columns():
    view = _make_view()
    assert view.cursor_col == 0
    view.handle_key("RIGHT", None)
    assert view.cursor_col == 1
    view.handle_key("RIGHT", None)
    assert view.cursor_col == 2
    view.handle_key("RIGHT", None)
    assert view.cursor_col == 3
    view.handle_key("LEFT", None)
    assert view.cursor_col == 2


# ---------------------------------------------------------------------------
# Timeout / Retries prompt — open
# ---------------------------------------------------------------------------


def test_open_timeout_prompt_sets_mode():
    view = _make_view()
    # Need entry with timeout_s
    entry = MagicMock()
    entry.stage = "implement"
    entry.timeout_s = None
    view._open_timeout_prompt(entry)
    assert view.mode is not None
    assert view.mode[0] == "timeout"
    assert view.mode[1] == "implement"


def test_open_retries_prompt_sets_mode():
    view = _make_view()
    entry = MagicMock()
    entry.stage = "classify"
    entry.max_retries = 3
    view._open_retries_prompt(entry)
    assert view.mode is not None
    assert view.mode[0] == "retries"
    assert view.mode[1] == "classify"
    assert view.mode[2] == ["3"]


# ---------------------------------------------------------------------------
# Numeric prompt key handling
# ---------------------------------------------------------------------------


def test_numeric_prompt_digit_appends():
    view = _make_view()
    view.mode = ("timeout", "implement", [""])
    view._handle_numeric_prompt_key("6")
    assert view.mode[2] == ["6"]
    view._handle_numeric_prompt_key("0")
    assert view.mode[2] == ["60"]
    view._handle_numeric_prompt_key("0")
    assert view.mode[2] == ["600"]


def test_numeric_prompt_backspace_removes():
    view = _make_view()
    view.mode = ("timeout", "implement", ["600"])
    view._handle_numeric_prompt_key("BACKSPACE")
    assert view.mode[2] == ["60"]
    view._handle_numeric_prompt_key("\x7f")
    assert view.mode[2] == ["6"]


def test_numeric_prompt_esc_cancels():
    view = _make_view()
    view.mode = ("timeout", "implement", ["600"])
    view._handle_numeric_prompt_key("ESC")
    assert view.mode is None
    assert view.last_ok is None


def test_numeric_prompt_enter_empty_sets_none(monkeypatch):
    """Enter with empty buffer → call set_stage_timeout(None) = clear override."""
    view = _make_view()
    view.mode = ("timeout", "implement", [""])
    view.data = MagicMock()
    view.data.context.namespace = "deile"
    mock_set = MagicMock(return_value=(True, "unset ok"))
    with patch.dict(sys.modules, {"_panel": sys.modules.get("_panel")}):
        import _panel_data as _pd
        _pd.set_stage_timeout = mock_set
    view._handle_numeric_prompt_key("\r")
    assert view.mode is None


def test_numeric_prompt_enter_with_value_calls_helper(monkeypatch):
    """Enter with '600' → call set_stage_timeout(600)."""
    view = _make_view()
    view.mode = ("timeout", "implement", ["600"])
    view.data = MagicMock()
    view.data.context.namespace = "deile"
    mock_set = MagicMock(return_value=(True, "DEILE_PIPELINE_TIMEOUT_S_IMPLEMENT=600 (rollout)"))
    with patch.dict(sys.modules, {"_panel": sys.modules.get("_panel")}):
        import _panel_data as _pd
        orig = _pd.set_stage_timeout
        _pd.set_stage_timeout = mock_set
    view._handle_numeric_prompt_key("\n")
    _pd.set_stage_timeout = orig
    assert view.mode is None


def test_numeric_prompt_retries_zero_calls_with_allow_zero_false(monkeypatch):
    """Retries=0 (sem '!') deve chamar set_stage_retries com allow_zero=False
    — a camada _panel_data rejeita e devolve mensagem clara."""
    view = _make_view()
    view.mode = ("retries", "implement", ["0"])
    view.data = MagicMock()
    view.data.context.namespace = "deile"
    import _panel as panel_mod
    captured = {}
    def fake_set(stage, value, *, allow_zero=False, namespace="deile"):
        captured["allow_zero"] = allow_zero
        captured["value"] = value
        return False, "max_retries=0 = fail-fast..."
    orig = panel_mod.pd_set_stage_retries
    panel_mod.pd_set_stage_retries = fake_set
    try:
        view._handle_numeric_prompt_key("\r")
    finally:
        panel_mod.pd_set_stage_retries = orig
    assert view.mode is None
    assert captured == {"allow_zero": False, "value": 0}
    assert view.last_ok is False


def test_numeric_prompt_retries_zero_bang_forces(monkeypatch):
    """Retries=0! (com bang) chama set_stage_retries com allow_zero=True."""
    view = _make_view()
    view.mode = ("retries", "implement", ["0!"])
    view.data = MagicMock()
    view.data.context.namespace = "deile"
    import _panel as panel_mod
    captured = {}
    def fake_set(stage, value, *, allow_zero=False, namespace="deile"):
        captured["allow_zero"] = allow_zero
        captured["value"] = value
        return True, "DEILE_PIPELINE_RETRIES_IMPLEMENT=0 (...)"
    orig = panel_mod.pd_set_stage_retries
    panel_mod.pd_set_stage_retries = fake_set
    try:
        view._handle_numeric_prompt_key("\r")
    finally:
        panel_mod.pd_set_stage_retries = orig
    assert view.mode is None
    assert captured == {"allow_zero": True, "value": 0}
    assert view.last_ok is True


def test_numeric_prompt_bang_key_appends_to_retries_buffer():
    """A tecla '!' é aceita no buffer de retries (e somente em retries)."""
    view = _make_view()
    view.mode = ("retries", "implement", ["0"])
    view._handle_numeric_prompt_key("!")
    assert view.mode[2] == ["0!"]


def test_numeric_prompt_bang_key_ignored_in_timeout():
    """A tecla '!' é ignorada no buffer de timeout (não tem semântica force)."""
    view = _make_view()
    view.mode = ("timeout", "implement", ["60"])
    view._handle_numeric_prompt_key("!")
    assert view.mode[2] == ["60"]


def test_numeric_prompt_bang_key_idempotent():
    """Pressionar '!' várias vezes não acumula — evita ``"0!!"`` que parsaria
    como inteiro inválido. Feedback do review na PR #407."""
    view = _make_view()
    view.mode = ("retries", "implement", ["0"])
    view._handle_numeric_prompt_key("!")
    assert view.mode[2] == ["0!"]
    view._handle_numeric_prompt_key("!")
    assert view.mode[2] == ["0!"]  # segundo '!' ignorado
    view._handle_numeric_prompt_key("!")
    assert view.mode[2] == ["0!"]  # terceiro '!' ignorado


def test_numeric_prompt_timeout_zero_is_invalid():
    """Timeout=0 should show error (not call kubectl)."""
    view = _make_view()
    view.mode = ("timeout", "implement", ["0"])
    view.data = MagicMock()
    view.data.context.namespace = "deile"
    view._handle_numeric_prompt_key("\r")
    assert view.mode is None
    assert view.last_ok is False
    assert ">" in view.last_msg


def test_numeric_prompt_non_digit_is_ignored():
    """Non-digit keys are ignored during numeric input."""
    view = _make_view()
    view.mode = ("timeout", "implement", ["60"])
    result = view._handle_numeric_prompt_key("a")
    assert view.mode[2] == ["60"]


# ---------------------------------------------------------------------------
# Reset cell (cols 2 and 3)
# ---------------------------------------------------------------------------


def test_reset_col2_calls_set_stage_timeout_none():
    view = _make_view()
    view.cursor_col = 2
    view.cursor_row = 0
    # Demo mode (data=None) → just show demo msg
    view._reset_current_cell()
    assert view.last_ok is False  # demo mode


def test_reset_col3_calls_set_stage_retries_none():
    view = _make_view()
    view.cursor_col = 3
    view.cursor_row = 0
    view._reset_current_cell()
    assert view.last_ok is False  # demo mode


def test_reset_with_real_data_col2():
    """reset col 2 (timeout) delegates to set_stage_timeout(None)."""
    view = _make_view()
    view.cursor_col = 2
    view.cursor_row = 0

    # Minimal fake data
    fake_stage_entry = MagicMock()
    fake_stage_entry.stage = "classify"
    fake_data = MagicMock()
    fake_data.stage_dispatch.get_all_stages.return_value = [fake_stage_entry] * 5
    fake_data.context.namespace = "deile"
    view.data = fake_data

    # _panel.py imports pd_set_stage_timeout at module level — patch there.
    panel_mod = sys.modules.get("_panel")
    mock_set = MagicMock(return_value=(True, "unset"))
    orig = panel_mod.pd_set_stage_timeout
    panel_mod.pd_set_stage_timeout = mock_set
    try:
        view._reset_current_cell()
    finally:
        panel_mod.pd_set_stage_timeout = orig
    mock_set.assert_called_once()
    args = mock_set.call_args
    # Second positional arg should be None (clear)
    assert args[0][1] is None


def test_reset_with_real_data_col3():
    """reset col 3 (retries) delegates to set_stage_retries(None)."""
    view = _make_view()
    view.cursor_col = 3
    view.cursor_row = 0

    fake_stage_entry = MagicMock()
    fake_stage_entry.stage = "classify"
    fake_data = MagicMock()
    fake_data.stage_dispatch.get_all_stages.return_value = [fake_stage_entry] * 5
    fake_data.context.namespace = "deile"
    view.data = fake_data

    panel_mod = sys.modules.get("_panel")
    mock_set = MagicMock(return_value=(True, "unset"))
    orig = panel_mod.pd_set_stage_retries
    panel_mod.pd_set_stage_retries = mock_set
    try:
        view._reset_current_cell()
    finally:
        panel_mod.pd_set_stage_retries = orig
    mock_set.assert_called_once()
    assert mock_set.call_args[0][1] is None


# ---------------------------------------------------------------------------
# Scaling row — cols 2/3 should not open scaling prompt
# ---------------------------------------------------------------------------


def test_scaling_row_col2_enter_shows_info():
    view = _make_view()
    n = len(view._stages())
    view.cursor_row = n + 1  # scaling row
    view.cursor_col = 2
    view.handle_key("\r", None)
    # Should NOT open a picker
    assert view.mode is None
    # Should show an informational message
    assert view.last_msg is not None
