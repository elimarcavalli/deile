"""Testes do login OAuth in-pod de um CLI worker da frota via painel ([O]).

A :class:`DispatchMatrixView` (tecla ``[d]``) ganha a tecla ``[O]`` — espelha o
``[L]`` do claude-worker, mas para a frota CLI. Quando a célula/worker
selecionado é um worker **oauth-capable** (``adapter.oauth is not None`` — hoje
``codex``), ``[O]`` suspende o painel e roda o device-auth interativo in-pod via
``deploy.py k8s cli-worker-login <kind> --in-pod``.

Cobertura:
  * worker oauth-capable selecionado + ``[O]`` → ``ActionResult.suspend`` com o
    argv certo (``cli-worker-login codex --in-pod``, namespace do painel);
  * worker env-auth (``opencode``/``aider``/...) → NÃO suspende; mensagem
    "usa chave de API (env-auth), não OAuth";
  * ``claude-worker`` → NÃO suspende; aponta o fluxo dedicado ``[L]``;
  * ``deile-worker`` → NÃO suspende; mensagem informativa;
  * a legenda da view documenta a tecla ``[O]``.

Mirror do estilo de ``test_panel_dispatch_mode.py`` — mesma injeção de
``sys.path`` para ``_panel`` / ``_panel_data`` resolverem sem o pacote DEILE.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# infra/k8s não é package — adicionar a sys.path (mirror de ``deploy.py panel``).
_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))


def _view_with_worker(worker: str, *, namespace: str = "deile"):
    """Monta uma :class:`DispatchMatrixView` cujo stage 0 usa ``worker``.

    ``data`` é um MagicMock cujo ``stage_dispatch.get_all_stages()`` devolve uma
    entry real (:class:`StageDispatchEntry`) com o worker pedido, e
    ``context.namespace`` setado — o suficiente para exercitar o roteamento de
    ``[O]`` sem cluster. O cursor fica na célula Worker (col 0) do stage 0.
    """
    from _panel import DispatchMatrixView
    from _panel_data import StageDispatchEntry

    entry = StageDispatchEntry(
        stage="classify",
        worker=worker,
        model=None,
        source="env",
    )
    data = MagicMock()
    data.context.namespace = namespace
    data.stage_dispatch.get_all_stages.return_value = [entry]

    view = DispatchMatrixView(data=data)
    view.cursor_row = 0  # stage row "classify"
    view.cursor_col = 0  # coluna Worker
    return view


class TestOauthCapableWorkerSuspend:
    """Worker oauth-capable (codex) → ``[O]`` suspende e roda o login in-pod."""

    def test_o_on_codex_worker_suspends_with_in_pod_login(self):
        from _panel import Action

        view = _view_with_worker("codex-worker", namespace="deile-gl")
        result = view.handle_key("O", MagicMock())

        assert result.kind == Action.SUSPEND
        cmd = result.payload["command"]
        # argv: python deploy.py --namespace deile-gl --yes k8s
        #       cli-worker-login codex --in-pod
        assert "cli-worker-login" in cmd
        assert "codex" in cmd
        assert "--in-pod" in cmd
        # namespace do painel propagado.
        assert "deile-gl" in cmd
        ns_idx = cmd.index("--namespace")
        assert cmd[ns_idx + 1] == "deile-gl"
        # deploy.py é o entrypoint e roda sob o python corrente.
        assert cmd[0] == sys.executable
        assert any(str(c).endswith("deploy.py") for c in cmd)
        # feedback "abrindo login" antes da suspensão, sem vazar credencial.
        assert "codex" in view.last_msg
        assert "device-auth" in view.last_msg.lower()

    def test_oauth_login_command_argv_shape(self):
        """A função pura que monta o argv é determinística e bem-formada."""
        view = _view_with_worker("codex-worker")
        cmd = view._oauth_login_command("codex", "deile")
        assert cmd[0] == sys.executable
        assert cmd[-4:] == ["k8s", "cli-worker-login", "codex", "--in-pod"]
        assert "--yes" in cmd
        assert cmd[cmd.index("--namespace") + 1] == "deile"


class TestEnvAuthWorkerNoSuspend:
    """Worker env-auth (opencode/aider/...) → ``[O]`` NÃO suspende."""

    def test_o_on_opencode_worker_shows_api_key_message(self):
        from _panel import Action

        view = _view_with_worker("opencode-worker")
        result = view.handle_key("O", MagicMock())

        # Não suspende — env-auth não tem device-auth OAuth.
        assert result.kind != Action.SUSPEND
        assert view.last_ok is None
        msg = view.last_msg.lower()
        assert "api" in msg
        assert "oauth" in msg


class TestCoreWorkersNoSuspend:
    """Workers núcleo (claude-worker / deile-worker) → ``[O]`` NÃO suspende."""

    def test_o_on_claude_worker_points_to_L(self):
        from _panel import Action

        view = _view_with_worker("claude-worker")
        result = view.handle_key("O", MagicMock())

        assert result.kind != Action.SUSPEND
        # claude tem fluxo OAuth próprio — a mensagem aponta [L].
        assert "[L]" in view.last_msg

    def test_o_on_deile_worker_informative(self):
        from _panel import Action

        view = _view_with_worker("deile-worker")
        result = view.handle_key("O", MagicMock())

        assert result.kind != Action.SUSPEND
        assert view.last_ok is None
        assert view.last_msg  # mensagem informativa não-vazia


class TestOauthCapableKindHelper:
    """Helper estático ``_oauth_capable_kind`` — single-source de oauth-capability."""

    def test_codex_worker_is_oauth_capable(self):
        from _panel import DispatchMatrixView

        assert DispatchMatrixView._oauth_capable_kind("codex-worker") == "codex"

    def test_env_auth_worker_is_not_oauth_capable(self):
        from _panel import DispatchMatrixView

        assert DispatchMatrixView._oauth_capable_kind("opencode-worker") is None

    def test_core_workers_are_not_oauth_capable(self):
        from _panel import DispatchMatrixView

        assert DispatchMatrixView._oauth_capable_kind("claude-worker") is None
        assert DispatchMatrixView._oauth_capable_kind("deile-worker") is None

    def test_garbage_worker_is_none(self):
        from _panel import DispatchMatrixView

        assert DispatchMatrixView._oauth_capable_kind("not-a-worker") is None
        assert DispatchMatrixView._oauth_capable_kind(None) is None
        assert DispatchMatrixView._oauth_capable_kind("") is None


class TestLegendDocumentsKey:
    def test_hotkeys_legend_mentions_O(self):
        from _panel import DispatchMatrixView

        assert "[O]" in DispatchMatrixView.HOTKEYS
        assert "OAuth" in DispatchMatrixView.HOTKEYS


class TestDemoModeNoOp:
    """Sem cluster (``data=None``) → ``[O]`` é no-op informativo (não suspende)."""

    def test_demo_mode_codex_no_suspend(self):
        from _panel import Action, DispatchMatrixView

        # Demo entries: _entries() devolve stages com worker "deile-worker",
        # então forçamos um worker oauth-capable só para exercitar o branch demo.
        view = DispatchMatrixView(data=None)
        # Substitui _selected_worker para devolver codex-worker (demo).
        view._selected_worker = lambda: "codex-worker"  # type: ignore[method-assign]
        result = view.handle_key("O", MagicMock())
        assert result.kind != Action.SUSPEND
        assert view.last_ok is False
        assert "demo" in view.last_msg.lower()
