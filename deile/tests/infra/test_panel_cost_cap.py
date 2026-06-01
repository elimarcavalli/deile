"""Tests for per-stage cost cap panel data functions (issue #392).

Covers:
- set_stage_cost_cap_usd: stage validation, value validation, kubectl absent,
  kubectl success, kubectl failure
- reset_stage_cost_cap_usd: stage validation, kubectl absent, success
- StageDispatchEntry.cost_cap_usd field present and read by StageDispatchProvider
- DispatchMatrixView: col 2 renders cost cap, reset_current_cell col 2 calls reset
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))

import _panel_data as pd_mod  # noqa: E402


def _deployment_env_json(envs: dict) -> dict:
    env_list = [{"name": k, "value": v} for k, v in envs.items()]
    return {"spec": {"template": {"spec": {"containers": [{"env": env_list}]}}}}


# ---------------------------------------------------------------------------
# set_stage_cost_cap_usd
# ---------------------------------------------------------------------------


class TestSetStageCostCapUsd:
    def test_invalid_stage_returns_error(self):
        ok, msg = pd_mod.set_stage_cost_cap_usd("bad_stage", "5.00")
        assert ok is False
        assert "inválido" in msg.lower() or "invalid" in msg.lower()

    def test_invalid_value_negative(self):
        ok, msg = pd_mod.set_stage_cost_cap_usd("implement", "-1.00")
        assert ok is False

    def test_invalid_value_non_numeric(self):
        ok, msg = pd_mod.set_stage_cost_cap_usd("implement", "abc")
        assert ok is False

    def test_kubectl_not_found(self):
        with patch("_panel_data.kubectl_bin", return_value=None):
            ok, msg = pd_mod.set_stage_cost_cap_usd("implement", "5.00")
        assert ok is False
        assert "kubectl" in msg.lower()

    def test_kubectl_success(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "deployment.apps/deile-pipeline env updated"
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("subprocess.run", return_value=mock_proc) as mock_run:
            ok, msg = pd_mod.set_stage_cost_cap_usd("implement", "5.00",
                                                     namespace="deile")
        assert ok is True
        assert "5.00" in msg
        # Check env var name in argv
        call_args = mock_run.call_args[0][0]
        env_arg = next((a for a in call_args if "COST_CAP" in a), None)
        assert env_arg == "DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT=5.00"

    def test_kubectl_failure(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Error: deployment not found"
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("subprocess.run", return_value=mock_proc):
            ok, msg = pd_mod.set_stage_cost_cap_usd("implement", "5.00")
        assert ok is False
        assert "not found" in msg.lower()

    def test_all_stages_accepted(self):
        """All five canonical stages should be accepted."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "ok"
        for stage in ("classify", "refine", "implement", "pr_review", "follow_ups"):
            with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
                 patch("subprocess.run", return_value=mock_proc):
                ok, _ = pd_mod.set_stage_cost_cap_usd(stage, "2.00")
            assert ok is True, f"stage {stage!r} should be accepted"


# ---------------------------------------------------------------------------
# reset_stage_cost_cap_usd
# ---------------------------------------------------------------------------


class TestResetStageCostCapUsd:
    def test_invalid_stage(self):
        ok, msg = pd_mod.reset_stage_cost_cap_usd("nope")
        assert ok is False

    def test_kubectl_not_found(self):
        with patch("_panel_data.kubectl_bin", return_value=None):
            ok, msg = pd_mod.reset_stage_cost_cap_usd("implement")
        assert ok is False

    def test_success_uses_trailing_dash(self):
        """Clear syntax: ``kubectl set env ... VAR-``."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "ok"
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("subprocess.run", return_value=mock_proc) as mock_run:
            ok, msg = pd_mod.reset_stage_cost_cap_usd("implement",
                                                       namespace="deile")
        assert ok is True
        call_args = mock_run.call_args[0][0]
        # Trailing dash = unset
        assert "DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT-" in call_args


# ---------------------------------------------------------------------------
# StageDispatchEntry cost_cap_usd field
# ---------------------------------------------------------------------------


class TestStageDispatchEntryCostCapField:
    def test_entry_has_cost_cap_field(self):
        entry = pd_mod.StageDispatchEntry(
            stage="implement",
            worker="deile-worker",
            model=None,
            source="default",
            cost_cap_usd="5.00",
        )
        assert entry.cost_cap_usd == "5.00"

    def test_entry_default_cost_cap_is_none(self):
        entry = pd_mod.StageDispatchEntry(
            stage="implement",
            worker="deile-worker",
            model=None,
            source="default",
        )
        assert entry.cost_cap_usd is None


class TestStageDispatchProviderReadsCostCap:
    def _pipeline_env_json(self, envs: dict) -> dict:
        return _deployment_env_json(envs)

    def test_cost_cap_read_from_pipeline_env(self):
        """Provider reads DEILE_PIPELINE_COST_CAP_USD_<STAGE> from deile-pipeline."""
        pipeline_json = self._pipeline_env_json({
            "DEILE_PIPELINE_COST_CAP_USD_IMPLEMENT": "7.50",
        })
        worker_json = _deployment_env_json({})

        def fake_capture(cmd, **kwargs):
            if "deile-pipeline" in " ".join(cmd):
                return pipeline_json
            if "deile-worker" in " ".join(cmd):
                return worker_json
            return None

        with patch("_panel_data._capture_json", side_effect=fake_capture), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            provider = pd_mod.StageDispatchProvider(enabled=True)
            entries = provider.get_all_stages(force=True)

        implement = next(e for e in entries if e.stage == "implement")
        assert implement.cost_cap_usd == "7.50"
        # Other stages have no cap
        classify = next(e for e in entries if e.stage == "classify")
        assert classify.cost_cap_usd is None

    def test_no_cost_cap_env_returns_none(self):
        """When no cost cap env is set, cost_cap_usd is None."""
        pipeline_json = _deployment_env_json({"SOME_OTHER_VAR": "x"})
        worker_json = _deployment_env_json({})

        def fake_capture(cmd, **kwargs):
            if "deile-pipeline" in " ".join(cmd):
                return pipeline_json
            if "deile-worker" in " ".join(cmd):
                return worker_json
            return None

        with patch("_panel_data._capture_json", side_effect=fake_capture), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            provider = pd_mod.StageDispatchProvider(enabled=True)
            entries = provider.get_all_stages(force=True)

        for entry in entries:
            assert entry.cost_cap_usd is None


# ---------------------------------------------------------------------------
# DispatchMatrixView – cost cap column rendering and reset
# ---------------------------------------------------------------------------


class TestDispatchMatrixViewCostCapCol:
    def _make_view(self, cost_cap_usd=None):
        """Build a DispatchMatrixView in demo mode with a single-entry list."""
        import _panel as panel_mod  # noqa: PLC0415

        view = panel_mod.DispatchMatrixView(data=None)
        # Override _entries() to return controllable demo data.
        mock_entry = pd_mod.StageDispatchEntry(
            stage="implement",
            worker="deile-worker",
            model=None,
            source="default",
            cost_cap_usd=cost_cap_usd,
        )
        view._entries = lambda: [mock_entry]  # type: ignore[method-assign]
        return view

    def test_column_count_includes_cost_cap(self):
        """Rendered table has a 'Cost cap (USD/run)' column header."""
        import _panel as panel_mod  # noqa: PLC0415
        from rich.console import Console  # noqa: PLC0415

        view = self._make_view()
        app = MagicMock()

        console = Console()
        with console.capture() as cap:
            console.print(view.render(app))
        output = cap.get()
        assert "Cost cap" in output or "cost_cap" in output.lower() or "USD/run" in output

    def test_no_cap_renders_no_cap_text(self):
        import _panel as panel_mod  # noqa: PLC0415
        from rich.console import Console  # noqa: PLC0415

        view = self._make_view(cost_cap_usd=None)
        app = MagicMock()
        console = Console()
        with console.capture() as cap:
            console.print(view.render(app))
        output = cap.get()
        assert "(no cap)" in output

    def test_cap_value_renders_with_dollar_sign(self):
        import _panel as panel_mod  # noqa: PLC0415
        from rich.console import Console  # noqa: PLC0415

        view = self._make_view(cost_cap_usd="5.00")
        app = MagicMock()
        console = Console()
        with console.capture() as cap:
            console.print(view.render(app))
        output = cap.get()
        assert "$5.00" in output

    def test_cursor_col_max_is_four(self):
        """RIGHT advances up to col 4 (Cost cap) and clamps there."""
        import _panel as panel_mod  # noqa: PLC0415

        view = self._make_view()
        view.cursor_col = 0
        app = MagicMock()

        for _ in range(10):
            view.handle_key("RIGHT", app)
        assert view.cursor_col == 4  # clamps at the Cost cap column

    def test_reset_col_4_calls_reset_cost_cap(self):
        """[r] on the cost-cap col (4) calls reset_stage_cost_cap_usd."""
        import _panel as panel_mod  # noqa: PLC0415

        view = self._make_view()
        view.cursor_row = 0
        view.cursor_col = 4  # cost cap column

        # Stub data with context
        mock_data = MagicMock()
        mock_data.context.namespace = "deile"
        view.data = mock_data
        view._entries = lambda: [  # type: ignore[method-assign]
            pd_mod.StageDispatchEntry("implement", "deile-worker", None,
                                      "default", cost_cap_usd=None)
        ]
        app = MagicMock()

        with patch("_panel.pd_reset_stage_cost_cap_usd",
                   return_value=(True, "ok")) as mock_reset:
            result = view.handle_key("r", app)

        mock_reset.assert_called_once_with("implement", namespace="deile")

    def test_enter_col_4_opens_cost_cap_picker(self):
        """[enter] on the cost-cap col (4) opens the cost_cap_usd picker modal."""
        import _panel as panel_mod  # noqa: PLC0415

        view = self._make_view(cost_cap_usd="5.00")
        view.cursor_row = 0
        view.cursor_col = 4  # cost cap column
        view.data = None  # demo mode
        app = MagicMock()

        view.handle_key("\r", app)
        assert view.mode is not None
        assert view.mode[0] == "cost_cap_usd"
        assert view.mode[1] == "implement"

    def test_picker_no_cap_calls_reset(self):
        """Selecting '(no cap)' in cost_cap picker calls reset_stage_cost_cap_usd."""
        import _panel as panel_mod  # noqa: PLC0415

        view = self._make_view()
        view.cursor_row = 0
        view.cursor_col = 4  # cost cap column
        mock_data = MagicMock()
        mock_data.context.namespace = "deile"
        view.data = mock_data
        view._entries = lambda: [  # type: ignore[method-assign]
            pd_mod.StageDispatchEntry("implement", "deile-worker", None,
                                      "default", cost_cap_usd=None)
        ]
        app = MagicMock()

        # Open picker
        view.handle_key("\r", app)
        assert view.mode is not None
        assert view.mode[0] == "cost_cap_usd"

        # picker_cursor=0 → "(no cap)"
        view.picker_cursor = 0

        with patch("_panel.pd_reset_stage_cost_cap_usd",
                   return_value=(True, "unset")) as mock_reset:
            view.handle_key("\r", app)

        mock_reset.assert_called_once_with("implement", namespace="deile")
        assert view.mode is None  # picker closed after selection
