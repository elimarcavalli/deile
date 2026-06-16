"""Tests for issue #488: _action_row_style predicate and activity-feed rendering.

Coverage:
  AC1. _action_row_style("routing.dropped") == "dim"
  AC1. _action_row_style("routing.mention") is None
  AC1. _action_row_style("routing.pr_unified") is None
  AC2. _activity_panel renders routing.dropped action cell with style "dim"
  AC2. _activity_panel renders routing.mention action cell with style None or ""
  AC2. _activity_panel renders routing.pr_unified action cell with style None or ""
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

_UTC = timezone.utc


def _make_event(action: str, detail: str = "") -> pd.ActivityEvent:
    ts = datetime.now(_UTC) - timedelta(seconds=1)
    return pd.ActivityEvent(
        ts=ts, actor="pipeline", action=action, target="#1", detail=detail
    )


def _make_data(events: list) -> MagicMock:
    data = MagicMock()
    state = pd.MultiSourceActivityState()
    state.events = events
    data.activity = MagicMock()
    data.activity.get.return_value = state
    return data


# ---------------------------------------------------------------------------
# AC1: predicado puro
# ---------------------------------------------------------------------------


class TestActionRowStylePredicate:
    def test_dropped_returns_dim(self):
        assert panel._action_row_style("routing.dropped") == "dim"

    def test_mention_returns_none(self):
        assert panel._action_row_style("routing.mention") is None

    def test_pr_unified_returns_none(self):
        assert panel._action_row_style("routing.pr_unified") is None

    def test_arbitrary_action_returns_none(self):
        assert panel._action_row_style("dispatch.started") is None

    def test_empty_string_returns_none(self):
        assert panel._action_row_style("") is None


# ---------------------------------------------------------------------------
# AC2: render — inspecionar células da coluna action
# ---------------------------------------------------------------------------


class TestActivityPanelActionCellStyle:
    def _get_action_cells(self, events: list):
        """Build DashboardView with given events and return action column cells."""
        data = _make_data(events)
        view = panel.DashboardView(data=data)
        p = view._activity_panel()
        tbl = p.renderable
        return list(tbl.columns[2]._cells)

    def test_dropped_action_cell_is_dim(self):
        ev = _make_event(action="routing.dropped")
        cells = self._get_action_cells([ev])
        assert len(cells) == 1
        cell = cells[0]
        from rich.text import Text

        assert isinstance(cell, Text)
        assert str(cell.style) == "dim"

    def test_mention_action_cell_has_no_style(self):
        ev = _make_event(action="routing.mention")
        cells = self._get_action_cells([ev])
        assert len(cells) == 1
        cell = cells[0]
        from rich.text import Text

        assert isinstance(cell, Text)
        assert cell.style in (None, "")

    def test_pr_unified_action_cell_has_no_style(self):
        ev = _make_event(action="routing.pr_unified")
        cells = self._get_action_cells([ev])
        assert len(cells) == 1
        cell = cells[0]
        from rich.text import Text

        assert isinstance(cell, Text)
        assert cell.style in (None, "")

    def test_dropped_and_real_in_same_table(self):
        """Dropped and real actions coexist without collision."""
        from rich.text import Text

        ts_base = datetime.now(_UTC)
        ev_drop = pd.ActivityEvent(
            ts=ts_base - timedelta(seconds=2),
            actor="pipeline",
            action="routing.dropped",
            target="#1",
            detail="skip: no assignee",
        )
        ev_real = pd.ActivityEvent(
            ts=ts_base - timedelta(seconds=1),
            actor="pipeline",
            action="routing.mention",
            target="#1",
            detail="",
        )
        # top() returns descending by ts: ev_real (newer) is cells[0]
        cells = self._get_action_cells([ev_drop, ev_real])
        assert len(cells) == 2
        action_styles = {str(c): c.style for c in cells if isinstance(c, Text)}
        assert action_styles.get("routing.dropped") == "dim"
        assert action_styles.get("routing.mention") in (None, "")

    def test_dropped_with_error_detail_action_cell_still_dim(self):
        """Error in detail (bold red on detail cell) does not affect action cell."""
        ev = _make_event(action="routing.dropped", detail="ERROR: something failed")
        cells = self._get_action_cells([ev])
        assert len(cells) == 1
        from rich.text import Text

        assert isinstance(cells[0], Text)
        assert str(cells[0].style) == "dim"
