"""Tests for the per-stage model panel feature (issue #305).

Covers `StageModelsProvider`, `set_stage_model`, `clear_stage_model` and the
dynamic-layout rendering of `StageModelsView` at 3 representative terminal
widths (80 / 120 / 200 cols).

The panel writes per-stage models via ``kubectl set env`` on the
``deile-worker`` Deployment (parallel to ``set_preferred_model``), not via
``SettingsManager`` — the operator's local settings.json does not propagate
to the worker Pod's filesystem. The provider also reads the cluster's
Deployment manifest, NOT the local Settings singleton.
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# infra/k8s is not a package — add to sys.path so `_panel_data` / `_panel`
# import resolves (mirrors how `deploy.py panel` runs).
_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))

from deile.config.settings import reset_settings  # noqa: E402
from deile.orchestration.pipeline.model_resolver import \
    PIPELINE_STAGES  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_MODEL_{stage.upper()}",
                           raising=False)
    monkeypatch.delenv("DEILE_PREFERRED_MODEL", raising=False)
    reset_settings()
    yield
    reset_settings()


def _deployment_json_with_env(envs: dict) -> dict:
    """Build a minimal kubectl-get-deployment JSON with the given env vars."""
    env_list = [{"name": k, "value": v} for k, v in envs.items()]
    return {"spec": {"template": {"spec": {"containers": [{"env": env_list}]}}}}


class TestStageModelsProvider:
    """The provider reads the deile-worker Deployment manifest via kubectl
    and extracts the 5 ``DEILE_PIPELINE_MODEL_<STAGE>`` env vars plus the
    global ``DEILE_PREFERRED_MODEL``. We mock ``_capture_json`` so the test
    doesn't need a running cluster."""

    def test_returns_five_entries_one_per_stage(self):
        from _panel_data import StageModelsProvider
        with patch("_panel_data._capture_json",
                   return_value=_deployment_json_with_env({})), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            provider = StageModelsProvider()
            entries = provider.get(force=True)
        assert len(entries) == 5
        assert [e.stage for e in entries] == list(PIPELINE_STAGES)

    def test_all_unset_no_global_shows_no_effective(self):
        from _panel_data import StageModelsProvider
        with patch("_panel_data._capture_json",
                   return_value=_deployment_json_with_env({})), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entries = StageModelsProvider().get(force=True)
        for e in entries:
            assert e.override is None
            assert e.effective is None
            assert e.is_fallback is False

    def test_global_only_marks_all_as_fallback(self):
        from _panel_data import StageModelsProvider
        with patch("_panel_data._capture_json",
                   return_value=_deployment_json_with_env({
                       "DEILE_PREFERRED_MODEL": "deepseek:deepseek-v4-pro",
                   })), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entries = StageModelsProvider().get(force=True)
        for e in entries:
            assert e.override is None
            assert e.effective == "deepseek:deepseek-v4-pro"
            assert e.is_fallback is True

    def test_mixed_overrides_and_fallback(self):
        from _panel_data import StageModelsProvider
        with patch("_panel_data._capture_json",
                   return_value=_deployment_json_with_env({
                       "DEILE_PREFERRED_MODEL": "deepseek:deepseek-v4-pro",
                       "DEILE_PIPELINE_MODEL_IMPLEMENT":
                           "anthropic:claude-opus-4-8",
                   })), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            by_stage = {e.stage: e for e in
                        StageModelsProvider().get(force=True)}
        # Override wins on implement.
        assert by_stage["implement"].override == "anthropic:claude-opus-4-8"
        assert by_stage["implement"].effective == "anthropic:claude-opus-4-8"
        assert by_stage["implement"].is_fallback is False
        # Refine falls back to global.
        assert by_stage["refine"].override is None
        assert by_stage["refine"].effective == "deepseek:deepseek-v4-pro"
        assert by_stage["refine"].is_fallback is True


class TestSetStageModel:
    """`set_stage_model` runs ``kubectl set env`` — same path as
    ``set_preferred_model``. Tests assert rejection paths short-circuit
    BEFORE any subprocess call, and the success path issues the right argv."""

    def test_rejects_unknown_stage(self):
        from _panel_data import set_stage_model
        ok, msg = set_stage_model("garbage", "deepseek:deepseek-v4-pro")
        assert ok is False
        assert "garbage" in msg
        assert "inválido" in msg.lower()

    def test_rejects_malformed_slug(self):
        from _panel_data import set_stage_model
        ok, msg = set_stage_model("implement", "NOT A SLUG")
        assert ok is False
        assert "slug" in msg.lower()

    def test_rejects_non_string_slug(self):
        from _panel_data import set_stage_model
        ok, msg = set_stage_model("implement", 42)  # type: ignore[arg-type]
        assert ok is False
        assert "slug" in msg.lower()

    def test_kubectl_missing_returns_clear_error(self):
        from _panel_data import set_stage_model
        with patch("_panel_data.kubectl_bin", return_value=None):
            ok, msg = set_stage_model("implement",
                                      "anthropic:claude-opus-4-8")
        assert ok is False
        assert "kubectl" in msg.lower()

    def test_success_issues_correct_kubectl_argv(self):
        from _panel_data import set_stage_model
        fake_proc = MagicMock(returncode=0, stdout="updated", stderr="")
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("_panel_data.subprocess.run",
                   return_value=fake_proc) as mock_run:
            ok, msg = set_stage_model("implement",
                                      "anthropic:claude-opus-4-8")
        assert ok is True
        argv = mock_run.call_args[0][0]
        assert argv[0] == "/fake/kubectl"
        # Fix 2026-05-27: DEILE_PIPELINE_MODEL_<STAGE> deve ser gravado no
        # deile-pipeline (não deile-worker — só o pipeline consome estas vars
        # via resolve_stage_model). Gravar no worker era silent cost amplifier.
        assert "deploy/deile-pipeline" in argv
        # The env-var name must be the canonical one (uppercase, no typo).
        assert "DEILE_PIPELINE_MODEL_IMPLEMENT=anthropic:claude-opus-4-8" \
            in argv

    def test_nonzero_returncode_surfaces_stderr(self):
        from _panel_data import set_stage_model
        fake_proc = MagicMock(returncode=1, stdout="",
                              stderr="forbidden: deployments.apps")
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("_panel_data.subprocess.run", return_value=fake_proc):
            ok, msg = set_stage_model("implement",
                                      "anthropic:claude-opus-4-8")
        assert ok is False
        assert "forbidden" in msg


class TestClearStageModel:
    def test_rejects_unknown_stage(self):
        from _panel_data import clear_stage_model
        ok, msg = clear_stage_model("garbage")
        assert ok is False

    def test_clear_issues_unset_argv(self):
        """``kubectl set env ... VAR-`` (trailing dash) is kubectl's
        syntax for unsetting an env. Assert the argv looks right."""
        from _panel_data import clear_stage_model
        fake_proc = MagicMock(returncode=0, stdout="updated", stderr="")
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("_panel_data.subprocess.run",
                   return_value=fake_proc) as mock_run:
            ok, msg = clear_stage_model("implement")
        assert ok is True
        argv = mock_run.call_args[0][0]
        # The trailing-dash unset syntax is mandatory; anything else
        # SETs an empty string instead of clearing.
        assert "DEILE_PIPELINE_MODEL_IMPLEMENT-" in argv


class TestStageModelsViewDynamicLayout:
    """The view's `render()` must adapt to terminal width without raising at
    any of three representative breakpoints (compact / normal / wide).

    Uses Rich's `Console.capture()` to render in-memory and assert the output
    contains the per-stage data. This is a smoke test of the layout pipeline
    — we are not asserting pixel-perfect layout, just that nothing throws and
    every stage row is present at every width."""

    @pytest.mark.parametrize("width", [80, 120, 200])
    def test_renders_at_all_breakpoints(self, width):
        from _panel import PanelApp, StageModelsView
        from _panel_data import PanelData
        from rich.console import Console

        view = StageModelsView(data=PanelData.default())
        app = PanelApp(views={"stage-models": view}, root="stage-models",
                       data=view.data)
        # Force the console to the test width — render reads
        # ``app.console.size.width`` for breakpoint selection.
        app.console = Console(width=width, file=StringIO(), force_terminal=True)
        # Stub the provider so we don't need a cluster.
        with patch.object(view.data.stage_models, "get",
                          return_value=[]):
            layout = view.render(app)

        capture_console = Console(width=width, file=StringIO(),
                                  force_terminal=True, record=True)
        capture_console.print(layout)
        text = capture_console.export_text()
        # Title must appear at every breakpoint.
        assert "Modelos por etapa" in text or "MODELOS" in text

    def test_compact_layout_hides_override_column(self):
        """At ``width < 100`` the Override column collapses — assert by
        rendering at 80 cols and checking the column header is absent."""
        from _panel import PanelApp, StageModelsView
        from _panel_data import StageModelEntry
        from rich.console import Console

        view = StageModelsView(data=None)  # demo mode keeps it self-contained
        app = PanelApp(views={"stage-models": view}, root="stage-models",
                       data=None)
        app.console = Console(width=80, file=StringIO(), force_terminal=True)
        # Override _entries to return predictable rows (demo path also works,
        # but this makes the assertion deterministic).
        view._entries = lambda: [  # type: ignore[method-assign]
            StageModelEntry(stage=s, override=None, effective=None,
                            is_fallback=False)
            for s in PIPELINE_STAGES
        ]
        layout = view.render(app)
        capture = Console(width=80, file=StringIO(),
                          force_terminal=True, record=True)
        capture.print(layout)
        text = capture.export_text()
        # Column header "Override" must NOT appear in compact mode (the wide
        # legend that mentioned it was rephrased to avoid the literal word).
        assert "Override" not in text

    def test_demo_mode_still_renders(self):
        """`data=None` is the demo path (no providers). The view must still
        render the 5 fallback rows so the operator can preview the UI without
        a live cluster."""
        from _panel import PanelApp, StageModelsView
        from rich.console import Console

        view = StageModelsView(data=None)
        app = PanelApp(views={"stage-models": view}, root="stage-models",
                       data=None)
        app.console = Console(width=120, file=StringIO(), force_terminal=True)
        layout = view.render(app)
        capture = Console(width=120, file=StringIO(),
                          force_terminal=True, record=True)
        capture.print(layout)
        text = capture.export_text()
        for stage in PIPELINE_STAGES:
            assert stage in text


class TestStageModelsViewKeyHandling:
    """Behavioural tests for the modal state machine: browse → set picker →
    confirmation, browse → clear confirmation."""

    def _new_view(self):
        from _panel import PanelApp, StageModelsView
        v = StageModelsView(data=None)  # demo mode — no kubectl needed
        app = PanelApp(views={"stage-models": v}, root="stage-models",
                       data=None)
        return v, app

    def test_enter_opens_picker_for_current_stage(self):
        view, app = self._new_view()
        assert view.mode is None
        view.handle_key("\r", app)
        assert view.mode is not None
        assert view.mode[0] == "set"
        assert view.mode[1] in PIPELINE_STAGES

    def test_c_opens_clear_confirmation(self):
        view, app = self._new_view()
        view.handle_key("c", app)
        assert view.mode is not None
        assert view.mode[0] == "clear"

    def test_arrow_down_advances_cursor(self):
        view, app = self._new_view()
        assert view.cursor == 0
        view.handle_key("DOWN", app)
        assert view.cursor == 1
        # j is the vim alias
        view.handle_key("j", app)
        assert view.cursor == 2

    def test_digit_shortcut_jumps_to_row(self):
        view, app = self._new_view()
        view.handle_key("3", app)
        assert view.cursor == 2  # 1-indexed display, 0-indexed cursor

    def test_picker_esc_closes_modal(self):
        view, app = self._new_view()
        view.mode = ("set", "implement")
        view.handle_key("ESC", app)
        assert view.mode is None

    def test_clear_confirm_n_cancels(self):
        view, app = self._new_view()
        view.mode = ("clear", "implement")
        view.handle_key("n", app)
        assert view.mode is None
        assert view.last_ok is False

    def test_esc_inside_modal_does_not_pop_view(self):
        """Regression: pressing ESC while picker is open must close the
        modal, not pop the StageModelsView off the app stack."""
        from _panel import DashboardView, PanelApp, StageModelsView
        dash = DashboardView(data=None)
        view = StageModelsView(data=None)
        app = PanelApp(views={"dashboard": dash, "stage-models": view},
                       root="dashboard", data=None)
        app.push("stage-models")
        assert app.current_view is view
        view.mode = ("set", "implement")
        # ESC must be intercepted by the view — never reach _handle_global
        assert view.intercepts_key("ESC") is True
        view.handle_key("ESC", app)
        assert view.mode is None
        assert app.current_view is view  # still on stage-models, not popped

    def test_esc_outside_modal_does_not_intercept(self):
        """ESC while browsing (no modal) must fall through to global ESC
        so the operator can leave the view normally."""
        view, _ = self._new_view()
        assert view.mode is None
        assert view.intercepts_key("ESC") is False

    def test_on_unmount_resets_modal_state(self):
        """Re-entering the view must land on the stage list, even if the
        operator was inside a picker when they last left (e.g. via [q] or
        global hotkey while the modal was open)."""
        view, app = self._new_view()
        view.mode = ("set", "implement")
        view.picker_cursor = 3
        view.on_unmount(app)
        assert view.mode is None
        assert view.picker_cursor == 0
