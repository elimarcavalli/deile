"""Tests for the pipeline dispatch-mode panel feature (issue #309).

Cobertura:
  * ``DispatchModeProvider`` — lê ``DEILE_PIPELINE_DISPATCH_MODE`` da
    Deployment ``deile-pipeline`` via kubectl get -o json (mockado), tanto
    quando a env var existe quanto quando cai no default do ConfigMap.
  * ``set_pipeline_dispatch_mode`` / ``clear_pipeline_dispatch_mode`` —
    validação, kubectl ausente, sucesso (kubectl set env com argv correto),
    falha (returncode != 0).
  * ``DispatchModeView`` — modal state machine (browse → set confirm,
    browse → clear confirm) e cancelamento default-deny.
  * ``build_implementer`` — emite warning quando ``dispatch_mode=claude``
    mas ``shutil.which("claude")`` retorna ``None`` (gap operacional
    documentado da issue #309).

Mirrors o estilo de ``test_panel_models_per_stage.py`` (issue #305) — mesma
estratégia de sys.path injection, mesmas fixtures, mesmas convenções.
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# infra/k8s não é um package — adicionar a sys.path para ``_panel_data`` /
# ``_panel`` resolverem (mirror de como ``deploy.py panel`` invoca o módulo).
_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))

from deile.config.settings import reset_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """Garante que env vars do host não contaminam os testes."""
    monkeypatch.delenv("DEILE_PIPELINE_DISPATCH_MODE", raising=False)
    reset_settings()
    yield
    reset_settings()


def _deployment_json_with_env(envs: dict) -> dict:
    """Builda um kubectl-get-deployment JSON minimal com as env vars dadas."""
    env_list = [{"name": k, "value": v} for k, v in envs.items()]
    return {"spec": {"template": {"spec": {"containers": [{"env": env_list}]}}}}


# ---------------------------------------------------------------------------
# DispatchModeProvider
# ---------------------------------------------------------------------------


class TestDispatchModeProvider:
    """O provider faz um único ``kubectl get -o json`` na Deployment
    ``deile-pipeline`` e extrai a env var. Mock o ``_capture_json`` para
    não precisar de cluster.
    """

    def test_env_var_set_returns_env_source(self):
        from _panel_data import DispatchModeProvider
        with patch("_panel_data._capture_json",
                   return_value=_deployment_json_with_env({
                       "DEILE_PIPELINE_DISPATCH_MODE": "claude",
                   })), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entry = DispatchModeProvider().get(force=True)
        assert entry.mode == "claude"
        assert entry.source == "env"
        assert entry.effective == "claude"

    def test_env_var_absent_falls_back_to_configmap_default(self):
        from _panel_data import DispatchModeProvider
        with patch("_panel_data._capture_json",
                   return_value=_deployment_json_with_env({
                       "SOME_OTHER_ENV": "x",
                   })), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entry = DispatchModeProvider().get(force=True)
        assert entry.mode is None
        assert entry.source == "default"
        # Default do ConfigMap deile-runtime-config.
        assert entry.effective == "deile_worker"

    def test_env_var_blank_treated_as_unset(self):
        """Quando a env existe com valor vazio (raro mas legal no k8s),
        o provider deve cair no default — não devolver string vazia como
        ``mode``."""
        from _panel_data import DispatchModeProvider
        with patch("_panel_data._capture_json",
                   return_value=_deployment_json_with_env({
                       "DEILE_PIPELINE_DISPATCH_MODE": "   ",
                   })), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entry = DispatchModeProvider().get(force=True)
        assert entry.mode is None
        assert entry.source == "default"

    def test_value_normalized_to_lowercase(self):
        """Mesmo que alguém edite manualmente o Deployment com `CLAUDE`,
        o provider devolve a forma canônica `claude` — para a view não
        ficar comparando case-sensitively contra os modos."""
        from _panel_data import DispatchModeProvider
        with patch("_panel_data._capture_json",
                   return_value=_deployment_json_with_env({
                       "DEILE_PIPELINE_DISPATCH_MODE": "CLAUDE",
                   })), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entry = DispatchModeProvider().get(force=True)
        assert entry.mode == "claude"


# ---------------------------------------------------------------------------
# set_pipeline_dispatch_mode
# ---------------------------------------------------------------------------


class TestSetPipelineDispatchMode:
    """``set_pipeline_dispatch_mode`` executa kubectl set env — mesmo caminho
    de ``set_preferred_model`` / ``set_stage_model``. Asserts que paths de
    rejeição curto-circuitam ANTES de qualquer subprocess, e que o caminho
    feliz emite a argv certa.
    """

    def test_rejects_unknown_mode(self):
        from _panel_data import set_pipeline_dispatch_mode
        ok, msg = set_pipeline_dispatch_mode("garbage")
        assert ok is False
        assert "garbage" in msg
        assert "inválido" in msg.lower()

    def test_rejects_empty_string(self):
        from _panel_data import set_pipeline_dispatch_mode
        ok, msg = set_pipeline_dispatch_mode("")
        assert ok is False
        assert "inválido" in msg.lower()

    def test_rejects_non_string(self):
        from _panel_data import set_pipeline_dispatch_mode
        ok, msg = set_pipeline_dispatch_mode(42)  # type: ignore[arg-type]
        assert ok is False

    def test_rejects_typo_variants(self):
        """Aliases internos (`worker`, `claude_code`) válidos para
        ``build_implementer`` NÃO são válidos para o painel — manter o
        conjunto canônico fechado em (claude | deile_worker) evita
        confusão na UI."""
        from _panel_data import set_pipeline_dispatch_mode
        ok, _ = set_pipeline_dispatch_mode("worker")
        assert ok is False
        ok, _ = set_pipeline_dispatch_mode("claude_code")
        assert ok is False
        ok, _ = set_pipeline_dispatch_mode("deile-worker")  # com hyphen
        assert ok is False

    def test_kubectl_missing_returns_clear_error(self):
        from _panel_data import set_pipeline_dispatch_mode
        with patch("_panel_data.kubectl_bin", return_value=None):
            ok, msg = set_pipeline_dispatch_mode("claude")
        assert ok is False
        assert "kubectl" in msg.lower()

    def test_success_issues_correct_kubectl_argv(self):
        from _panel_data import set_pipeline_dispatch_mode
        fake_proc = MagicMock(returncode=0, stdout="updated", stderr="")
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("_panel_data.subprocess.run",
                   return_value=fake_proc) as mock_run:
            ok, _ = set_pipeline_dispatch_mode("claude")
        assert ok is True
        argv = mock_run.call_args[0][0]
        assert argv[0] == "/fake/kubectl"
        assert "deploy/deile-pipeline" in argv
        # A env var precisa estar em formato KEY=VALUE canônico.
        assert "DEILE_PIPELINE_DISPATCH_MODE=claude" in argv

    def test_success_normalizes_uppercase_input(self):
        """Operador digita `CLAUDE` na CLI — a função canonicaliza para
        `claude` antes de virar argv (sem case mismatch no Deployment)."""
        from _panel_data import set_pipeline_dispatch_mode
        fake_proc = MagicMock(returncode=0, stdout="updated", stderr="")
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("_panel_data.subprocess.run",
                   return_value=fake_proc) as mock_run:
            ok, _ = set_pipeline_dispatch_mode("CLAUDE")
        assert ok is True
        argv = mock_run.call_args[0][0]
        # Lowercase no argv, sem typo.
        assert "DEILE_PIPELINE_DISPATCH_MODE=claude" in argv

    def test_nonzero_returncode_surfaces_stderr(self):
        from _panel_data import set_pipeline_dispatch_mode
        fake_proc = MagicMock(returncode=1, stdout="",
                              stderr="forbidden: deployments.apps")
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("_panel_data.subprocess.run", return_value=fake_proc):
            ok, msg = set_pipeline_dispatch_mode("claude")
        assert ok is False
        assert "forbidden" in msg

    def test_namespace_passed_through(self):
        """O painel TUI suporta multi-NS (PR #315). A função deve respeitar
        o ``namespace=`` kwarg em vez de hardcoded ``NS``."""
        from _panel_data import set_pipeline_dispatch_mode
        fake_proc = MagicMock(returncode=0, stdout="updated", stderr="")
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("_panel_data.subprocess.run",
                   return_value=fake_proc) as mock_run:
            ok, _ = set_pipeline_dispatch_mode("claude", namespace="customns")
        assert ok is True
        argv = mock_run.call_args[0][0]
        # O argv tem '-n customns' (não o default 'deile').
        assert "customns" in argv


# ---------------------------------------------------------------------------
# clear_pipeline_dispatch_mode
# ---------------------------------------------------------------------------


class TestClearPipelineDispatchMode:
    def test_kubectl_missing_returns_clear_error(self):
        from _panel_data import clear_pipeline_dispatch_mode
        with patch("_panel_data.kubectl_bin", return_value=None):
            ok, msg = clear_pipeline_dispatch_mode()
        assert ok is False
        assert "kubectl" in msg.lower()

    def test_clear_issues_unset_argv(self):
        """``kubectl set env ... VAR-`` (trailing dash) é a sintaxe do
        kubectl para unset. Argv tem que carregar a forma com dash —
        qualquer outra coisa SETa string vazia em vez de limpar."""
        from _panel_data import clear_pipeline_dispatch_mode
        fake_proc = MagicMock(returncode=0, stdout="updated", stderr="")
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("_panel_data.subprocess.run",
                   return_value=fake_proc) as mock_run:
            ok, _ = clear_pipeline_dispatch_mode()
        assert ok is True
        argv = mock_run.call_args[0][0]
        # Trailing dash é mandatory.
        assert "DEILE_PIPELINE_DISPATCH_MODE-" in argv

    def test_nonzero_returncode_surfaces_stderr(self):
        from _panel_data import clear_pipeline_dispatch_mode
        fake_proc = MagicMock(returncode=1, stdout="",
                              stderr="forbidden: deployments.apps")
        with patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"), \
             patch("_panel_data.subprocess.run", return_value=fake_proc):
            ok, msg = clear_pipeline_dispatch_mode()
        assert ok is False
        assert "forbidden" in msg


# ---------------------------------------------------------------------------
# DispatchModeView
# ---------------------------------------------------------------------------


class TestDispatchModeViewRendering:
    @pytest.mark.parametrize("width", [80, 120, 200])
    def test_renders_at_all_breakpoints(self, width):
        from _panel import DispatchModeView, PanelApp
        from rich.console import Console

        view = DispatchModeView(data=None)  # demo mode
        app = PanelApp(views={"dispatch-mode": view}, root="dispatch-mode",
                       data=None)
        app.console = Console(width=width, file=StringIO(),
                              force_terminal=True)
        layout = view.render(app)
        capture = Console(width=width, file=StringIO(),
                          force_terminal=True, record=True)
        capture.print(layout)
        text = capture.export_text()
        # Header da view deve aparecer em todos os breakpoints.
        assert ("Modo de despacho" in text
                or "MODOS DISPONÍVEIS" in text)

    def test_demo_mode_shows_both_modes(self):
        from _panel import DispatchModeView, PanelApp
        from rich.console import Console

        view = DispatchModeView(data=None)
        app = PanelApp(views={"dispatch-mode": view}, root="dispatch-mode",
                       data=None)
        app.console = Console(width=140, file=StringIO(),
                              force_terminal=True)
        layout = view.render(app)
        capture = Console(width=140, file=StringIO(),
                          force_terminal=True, record=True)
        capture.print(layout)
        text = capture.export_text()
        # As 2 opções precisam aparecer na lista.
        assert "deile_worker" in text
        assert "claude" in text


class TestDispatchModeViewKeyHandling:
    def _new_view(self):
        from _panel import DispatchModeView, PanelApp
        v = DispatchModeView(data=None)  # demo mode — sem kubectl
        app = PanelApp(views={"dispatch-mode": v}, root="dispatch-mode",
                       data=None)
        return v, app

    def test_enter_opens_set_confirmation(self):
        view, app = self._new_view()
        assert view.mode_modal is None
        view.handle_key("\r", app)
        assert view.mode_modal is not None
        assert view.mode_modal[0] == "set"
        # Modo selecionado é o do cursor (default 0 → "deile_worker").
        assert view.mode_modal[1] == "deile_worker"

    def test_c_opens_clear_confirmation(self):
        view, app = self._new_view()
        view.handle_key("c", app)
        assert view.mode_modal is not None
        assert view.mode_modal[0] == "clear"

    def test_arrow_down_advances_cursor(self):
        view, app = self._new_view()
        assert view.cursor == 0
        view.handle_key("DOWN", app)
        assert view.cursor == 1
        # `j` é alias vim.
        view.handle_key("j", app)
        # 2 modos só → wraparound para 0.
        assert view.cursor == 0

    def test_digit_shortcut_jumps_to_row(self):
        view, app = self._new_view()
        view.handle_key("2", app)
        assert view.cursor == 1  # 1-indexed display → 0-indexed cursor
        view.handle_key("1", app)
        assert view.cursor == 0

    def test_set_confirm_y_applies(self):
        """A view chama ``set_pipeline_dispatch_mode`` apenas em data!=None,
        então em demo mode o ``_apply_set`` registra ``last_ok=False`` sem
        invocar kubectl."""
        view, app = self._new_view()
        view.cursor = 1  # claude
        view.handle_key("\r", app)
        assert view.mode_modal == ("set", "claude")
        view.handle_key("y", app)
        assert view.mode_modal is None
        # Demo mode → last_ok=False e mensagem "modo demo".
        assert view.last_ok is False
        assert "demo" in (view.last_msg or "").lower()

    def test_set_confirm_n_cancels(self):
        view, app = self._new_view()
        view.handle_key("\r", app)  # → modal set
        view.handle_key("n", app)
        assert view.mode_modal is None
        assert view.last_ok is False  # default-deny audit msg

    def test_set_confirm_unexpected_key_cancels_default_deny(self):
        """Qualquer tecla diferente de [y] cancela. Padrão default-deny
        mirror do ModelSwitcherView (issue #305)."""
        view, app = self._new_view()
        view.handle_key("\r", app)
        view.handle_key("Z", app)  # tecla aleatória
        assert view.mode_modal is None
        assert view.last_ok is False

    def test_clear_confirm_y_applies(self):
        view, app = self._new_view()
        view.handle_key("c", app)
        assert view.mode_modal == ("clear", None)
        view.handle_key("y", app)
        assert view.mode_modal is None
        assert view.last_ok is False  # demo mode
        assert "demo" in (view.last_msg or "").lower()

    def test_clear_confirm_n_cancels(self):
        view, app = self._new_view()
        view.handle_key("c", app)
        view.handle_key("n", app)
        assert view.mode_modal is None
        assert view.last_ok is False

    def test_esc_inside_modal_does_not_pop_view(self):
        """Regression: ESC dentro do modal precisa fechar o modal, não
        sair da view (mirror do test no StageModelsView)."""
        from _panel import DashboardView, DispatchModeView, PanelApp
        dash = DashboardView(data=None)
        view = DispatchModeView(data=None)
        app = PanelApp(views={"dashboard": dash, "dispatch-mode": view},
                       root="dashboard", data=None)
        app.push("dispatch-mode")
        assert app.current_view is view
        view.mode_modal = ("set", "claude")
        assert view.intercepts_key("ESC") is True
        view.handle_key("ESC", app)
        assert view.mode_modal is None
        assert app.current_view is view  # ainda na view, não popped

    def test_esc_outside_modal_does_not_intercept(self):
        view, _ = self._new_view()
        assert view.mode_modal is None
        assert view.intercepts_key("ESC") is False

    def test_on_unmount_resets_modal_state(self):
        """Operador sai da view (q/global hotkey) com modal aberto —
        re-entry deve aterrissar no estado limpo."""
        view, app = self._new_view()
        view.mode_modal = ("set", "claude")
        view.on_unmount(app)
        assert view.mode_modal is None


class TestDashboardHotkey:
    """O dashboard mapeia ``[d]`` para a dispatch-mode view (issue #309)."""

    def test_d_hotkey_navigates_to_dispatch_mode(self):
        from _panel import Action, DashboardView, PanelApp
        dash = DashboardView(data=None)
        app = PanelApp(views={"dashboard": dash}, root="dashboard", data=None)
        result = dash.handle_key("d", app)
        assert result.kind == Action.NAV
        assert result.target == "dispatch-mode"


# ---------------------------------------------------------------------------
# build_implementer claude-binary warning (issue #309)
# ---------------------------------------------------------------------------


class TestBuildImplementerClaudeWarning:
    """Quando ``dispatch_mode=claude`` mas o binary ``claude`` não está
    no PATH, ``build_implementer`` deve logar warning (sem fail-fast).

    Sem fail-fast porque o operador pode estar montando o binary via volume
    que ``shutil.which`` ainda não enxerga; a falha real surge no primeiro
    subprocess.

    Patcha o ``logger.warning`` do módulo implementer diretamente — caplog
    fica frágil sob a suite completa (alguns testes prévios reconfiguram o
    logger; ``propagate=False`` bloqueia caplog). Mock direto no logger
    sobrevive a qualquer pollution prévia.
    """

    def test_warning_emitted_when_claude_missing(self):
        from deile.orchestration.pipeline import implementer as impl_mod
        with patch.object(impl_mod, "shutil") as mock_shutil, \
             patch.object(impl_mod.logger, "warning") as mock_warn:
            mock_shutil.which.return_value = None
            implementer = impl_mod.build_implementer("claude")
        assert isinstance(implementer, impl_mod.ClaudeImplementer)
        # Pelo menos uma chamada de warning com a mensagem certa.
        assert mock_warn.called
        msg = mock_warn.call_args[0][0].lower()
        assert "claude" in msg
        assert ("não encontrado" in msg or "not found" in msg
                or "enoent" in msg)

    def test_no_warning_when_claude_present(self):
        from deile.orchestration.pipeline import implementer as impl_mod
        with patch.object(impl_mod, "shutil") as mock_shutil, \
             patch.object(impl_mod.logger, "warning") as mock_warn:
            mock_shutil.which.return_value = "/usr/local/bin/claude"
            implementer = impl_mod.build_implementer("claude")
        assert isinstance(implementer, impl_mod.ClaudeImplementer)
        # Sem warning sobre claude ausente — pode haver outro warning legítimo,
        # mas nenhum com "não encontrado".
        for call in mock_warn.call_args_list:
            text = (call[0][0] if call[0] else "").lower()
            assert not (
                "claude" in text
                and ("não encontrado" in text or "not found" in text)
            )

    def test_no_warning_when_worker_mode(self):
        """Modo worker não usa claude — não faz sentido emitir warning,
        mesmo que o binary esteja ausente."""
        from deile.orchestration.pipeline import implementer as impl_mod
        with patch.object(impl_mod, "shutil") as mock_shutil, \
             patch.object(impl_mod.logger, "warning") as mock_warn:
            mock_shutil.which.return_value = None
            impl_mod.build_implementer("deile_worker")
        # Worker mode não tem caminho que chame _warn_if_claude_unavailable.
        for call in mock_warn.call_args_list:
            text = (call[0][0] if call[0] else "").lower()
            assert not (
                "claude" in text
                and ("não encontrado" in text or "not found" in text)
            )

    def test_empty_dispatch_mode_defaults_claude_and_warns_if_missing(self):
        """Empty/None dispatch_mode legado cai em ClaudeImplementer — mesmo
        warning aplica."""
        from deile.orchestration.pipeline import implementer as impl_mod
        with patch.object(impl_mod, "shutil") as mock_shutil, \
             patch.object(impl_mod.logger, "warning") as mock_warn:
            mock_shutil.which.return_value = None
            implementer = impl_mod.build_implementer("")
        assert isinstance(implementer, impl_mod.ClaudeImplementer)
        assert mock_warn.called
        assert "claude" in mock_warn.call_args[0][0].lower()
