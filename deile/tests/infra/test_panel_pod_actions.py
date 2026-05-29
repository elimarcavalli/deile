"""Regression tests for the pod-picker lifecycle hotkeys (PR P6).

Cobertura:

1. **Helpers em ``_panel_data.py``** — ``delete_pod``,
   ``rollout_restart_deployment``, ``rollout_restart_all``,
   ``kill_local_pid`` — validações de entrada, audit, sucesso/falha
   do subprocess. Mocking de ``subprocess.run`` e ``os.kill`` evita
   bater no cluster real ou matar processo de verdade.

2. **PodPickerView** em ``_panel.py`` — fluxo de confirmação de
   ``x``/``r``/``R``, mapeamento role→deployment, parse de pid a partir
   do row local, cancelamento default-deny, mensagens de feedback.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

# ---------------------------------------------------------------------------
# delete_pod
# ---------------------------------------------------------------------------

class TestDeletePod:
    def test_invalid_name_denied(self):
        ok, msg = pd.delete_pod("not valid pod!")
        assert ok is False
        assert "inválido" in msg.lower()

    def test_invalid_type_denied(self):
        ok, msg = pd.delete_pod(12345)  # type: ignore[arg-type]
        assert ok is False
        assert "inválido" in msg.lower()

    def test_kubectl_missing_failed(self):
        with patch.object(pd, "kubectl_bin", return_value=None):
            ok, msg = pd.delete_pod("deile-worker-abc-xyz")
        assert ok is False
        assert "kubectl não encontrado" in msg

    def test_subprocess_success(self):
        fake = MagicMock(returncode=0, stdout='pod "x" deleted\n', stderr="")
        with patch.object(pd, "kubectl_bin", return_value="/kubectl"), \
             patch.object(pd.subprocess, "run", return_value=fake) as run:
            ok, msg = pd.delete_pod("deile-worker-abc-xyz")
        assert ok is True
        assert "deleted" in msg
        argv = run.call_args[0][0]
        assert argv[:5] == ["/kubectl", "-n", "deile", "delete", "pod"]
        assert argv[5] == "deile-worker-abc-xyz"

    def test_subprocess_nonzero_failed(self):
        fake = MagicMock(returncode=1, stdout="", stderr="not found\n")
        with patch.object(pd, "kubectl_bin", return_value="/kubectl"), \
             patch.object(pd.subprocess, "run", return_value=fake):
            ok, msg = pd.delete_pod("deile-worker-abc-xyz")
        assert ok is False
        assert "not found" in msg


# ---------------------------------------------------------------------------
# rollout_restart_deployment / _all
# ---------------------------------------------------------------------------

class TestRolloutRestart:
    def test_unknown_deployment_denied(self):
        ok, msg = pd.rollout_restart_deployment("malicious-deploy")
        assert ok is False
        assert "não permitido" in msg

    def test_kubectl_missing_failed(self):
        with patch.object(pd, "kubectl_bin", return_value=None):
            ok, msg = pd.rollout_restart_deployment("deile-worker")
        assert ok is False
        assert "kubectl não encontrado" in msg

    def test_success(self):
        fake = MagicMock(returncode=0,
                         stdout='deployment.apps/deile-worker restarted\n',
                         stderr="")
        with patch.object(pd, "kubectl_bin", return_value="/kubectl"), \
             patch.object(pd.subprocess, "run", return_value=fake) as run:
            ok, msg = pd.rollout_restart_deployment("deile-worker")
        assert ok is True
        assert "restarted" in msg
        argv = run.call_args[0][0]
        assert argv == ["/kubectl", "-n", "deile", "rollout", "restart",
                        "deployment/deile-worker"]

    def test_rollout_all_covers_full_whitelist(self):
        seen = []

        def _fake_one(dep, **kw):
            seen.append(dep)
            return True, f"{dep} OK"

        with patch.object(pd, "rollout_restart_deployment",
                          side_effect=_fake_one):
            results = pd.rollout_restart_all()
        assert {dep for dep, _, _ in results} == pd._ALLOWED_DEPLOYMENTS_FULL
        # ``seen`` veio em ordem determinística (sorted) — fix dependence-free.
        assert seen == sorted(pd._ALLOWED_DEPLOYMENTS_FULL)
        assert all(ok for _, ok, _ in results)

    def test_rollout_all_partial_failure_does_not_abort(self):
        def _fake_one(dep, **kw):
            return (dep != "deile-worker"), f"{dep} done"

        with patch.object(pd, "rollout_restart_deployment",
                          side_effect=_fake_one):
            results = pd.rollout_restart_all()
        # Uma falha não para o loop — todos os deployments do whitelist
        # aparecem no resultado (issue tmpfs resize adicionou claude-worker
        # ao whitelist, então o número não é fixo em 4).
        assert len(results) == len(pd._ALLOWED_DEPLOYMENTS_FULL)
        oks = {dep: ok for dep, ok, _ in results}
        assert oks["deile-worker"] is False
        assert all(ok for dep, ok in oks.items() if dep != "deile-worker")


# ---------------------------------------------------------------------------
# kill_local_pid
# ---------------------------------------------------------------------------

class TestKillLocalPid:
    def test_invalid_pid_denied(self):
        ok, msg = pd.kill_local_pid(0)
        assert ok is False
        assert "inválido" in msg.lower()

    def test_invalid_sig_denied(self):
        ok, msg = pd.kill_local_pid(12345, sig="SIGUSR1")
        assert ok is False
        assert "não permitido" in msg

    def test_pid_owned_by_other_user_denied(self):
        import psutil
        fake_proc = MagicMock()
        fake_proc.uids.return_value = MagicMock(real=99999)
        with patch.object(psutil, "Process", return_value=fake_proc), \
             patch.object(os, "getuid", return_value=os.getuid()):
            ok, msg = pd.kill_local_pid(12345)
        assert ok is False
        assert "outro usuário" in msg

    def test_pid_missing_failed(self):
        import psutil

        def _raise(_pid):
            raise psutil.NoSuchProcess(_pid)

        with patch.object(psutil, "Process", side_effect=_raise):
            ok, msg = pd.kill_local_pid(99999)
        assert ok is False
        assert "não existe" in msg

    def test_success_sigterm(self):
        import psutil
        fake_proc = MagicMock()
        fake_proc.uids.return_value = MagicMock(real=os.getuid())
        fake_proc.wait.return_value = None
        with patch.object(psutil, "Process", return_value=fake_proc), \
             patch.object(os, "kill") as kill_mock:
            ok, msg = pd.kill_local_pid(12345)
        assert ok is True
        assert "SIGTERM" in msg
        kill_mock.assert_called_once_with(12345, signal.SIGTERM)

    def test_sigterm_escalates_to_sigkill_on_timeout(self):
        import psutil
        fake_proc = MagicMock()
        fake_proc.uids.return_value = MagicMock(real=os.getuid())
        fake_proc.wait.side_effect = psutil.TimeoutExpired(seconds=5)
        with patch.object(psutil, "Process", return_value=fake_proc), \
             patch.object(os, "kill") as kill_mock:
            ok, msg = pd.kill_local_pid(12345)
        assert ok is True
        assert "SIGKILL" in msg
        # 1ª chamada SIGTERM, 2ª SIGKILL — sem dependência de ordem garantida
        # pelo call_args_list, mas é fácil de verificar.
        sent_signals = [c.args[1] for c in kill_mock.call_args_list]
        assert sent_signals == [signal.SIGTERM, signal.SIGKILL]


# ---------------------------------------------------------------------------
# PodPickerView interactions
# ---------------------------------------------------------------------------

def _row(name: str, role: str, **extra) -> panel.PodRow:
    return panel.PodRow(
        icon=extra.get("icon", "●"),
        name=name, role=role,
        status=extra.get("status", "Running"),
        age=extra.get("age", "5m"),
        restarts=extra.get("restarts", "0"),
        last_activity=extra.get("last_activity", "—"),
        doing_now=extra.get("doing_now", "idle"),
        busy=extra.get("busy", False),
    )


class TestPodPickerHotkeys:
    def _view_with_rows(self, rows):
        v = panel.PodPickerView()
        v._rows = lambda: rows  # type: ignore[assignment]
        v.data = MagicMock()    # truthy para passar do "modo demo"
        # PR #297: ações destrutivas leem o NS do RuntimeContext em vez de
        # cair no default. Configura o mock para devolver "deile" (default).
        v.data.context.namespace = "deile"
        return v

    def test_deployment_for_role_mapping(self):
        v = panel.PodPickerView()
        assert v._deployment_for_role("worker") == "deile-worker"
        assert v._deployment_for_role("pipeline") == "deile-pipeline"
        assert v._deployment_for_role("bot") == "deilebot"
        assert v._deployment_for_role("shell") == "deile-shell"
        assert v._deployment_for_role("local-deile") is None
        assert v._deployment_for_role("other") is None

    def test_pid_from_local_row(self):
        v = panel.PodPickerView()
        assert v._pid_from_local_row(_row("local-deile#42", "local-deile")) == 42
        assert v._pid_from_local_row(_row("worker-xyz", "worker")) is None
        assert v._pid_from_local_row(_row("local-bot#abc", "local-bot")) is None

    def test_x_on_k8s_pod_opens_confirmation(self):
        rows = [_row("deile-worker-abc-xyz", "worker")]
        v = self._view_with_rows(rows)
        result = v.handle_key("x", app=MagicMock())
        assert v.confirm_action == "x"
        assert result.kind.name == "REFRESH"

    def test_x_on_local_pid_opens_confirmation(self):
        rows = [_row("local-deile#1234", "local-deile")]
        v = self._view_with_rows(rows)
        v.handle_key("x", app=MagicMock())
        assert v.confirm_action == "x"

    def test_r_on_local_pid_rejects_immediately(self):
        rows = [_row("local-deile#1234", "local-deile")]
        v = self._view_with_rows(rows)
        v.handle_key("r", app=MagicMock())
        assert v.confirm_action is None
        assert "não suportado em processo local" in v.last_msg
        assert v.last_ok is False

    def test_r_on_k8s_pod_opens_confirmation(self):
        rows = [_row("deile-worker-abc-xyz", "worker")]
        v = self._view_with_rows(rows)
        v.handle_key("r", app=MagicMock())
        assert v.confirm_action == "r"

    def test_R_uppercase_opens_restart_all_confirmation(self):
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.handle_key("R", app=MagicMock())
        assert v.confirm_action == "R"

    def test_R_works_without_selection(self):
        """``R`` é global — não depende de pod selecionado."""
        v = self._view_with_rows([])
        v.handle_key("R", app=MagicMock())
        assert v.confirm_action == "R"

    def test_cancel_on_any_key_other_than_y(self):
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.confirm_action = "x"
        v.handle_key("n", app=MagicMock())
        assert v.confirm_action is None
        assert v.last_ok is None  # cancelado → não é "falha"
        assert "cancelado" in v.last_msg

    def test_apply_x_k8s_calls_delete_pod(self):
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.confirm_action = "x"
        with patch.object(pd, "delete_pod",
                          return_value=(True, 'pod "deile-worker-abc" deleted')) as dp:
            v.handle_key("y", app=MagicMock())
        # PR #297: NS é propagado do RuntimeContext via kwarg.
        dp.assert_called_once_with("deile-worker-abc", namespace="deile")
        assert v.last_ok is True
        assert v.confirm_action is None

    def test_apply_x_local_calls_kill(self):
        v = self._view_with_rows([_row("local-deile#42", "local-deile")])
        v.confirm_action = "x"
        with patch.object(pd, "kill_local_pid",
                          return_value=(True, "pid 42 encerrado via SIGTERM")) as k:
            v.handle_key("y", app=MagicMock())
        k.assert_called_once_with(42)
        assert v.last_ok is True

    def test_apply_r_calls_rollout_restart_with_correct_deployment(self):
        v = self._view_with_rows([_row("deilebot-xyz-abc", "bot")])
        v.confirm_action = "r"
        with patch.object(pd, "rollout_restart_deployment",
                          return_value=(True, "deployment.apps/deilebot restarted")) as rr:
            v.handle_key("y", app=MagicMock())
        # PR #297: NS é propagado do RuntimeContext via kwarg.
        rr.assert_called_once_with("deilebot", namespace="deile")
        assert v.last_ok is True

    def test_apply_R_calls_rollout_all(self):
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.confirm_action = "R"
        fake_results = [
            ("deilebot", True, "ok"),
            ("deile-pipeline", True, "ok"),
            ("deile-shell", True, "ok"),
            ("deile-worker", True, "ok"),
        ]
        with patch.object(pd, "rollout_restart_all",
                          return_value=fake_results) as ra:
            v.handle_key("y", app=MagicMock())
        ra.assert_called_once_with(namespace="deile")
        assert v.last_ok is True
        assert all(dep in v.last_msg
                   for dep, _, _ in fake_results)

    def test_apply_R_partial_failure_marks_last_ok_false(self):
        v = self._view_with_rows([])
        v.confirm_action = "R"
        fake_results = [
            ("deilebot", True, "ok"),
            ("deile-worker", False, "rpc error"),
            ("deile-pipeline", True, "ok"),
            ("deile-shell", True, "ok"),
        ]
        with patch.object(pd, "rollout_restart_all", return_value=fake_results):
            v.handle_key("y", app=MagicMock())
        assert v.last_ok is False
        assert "FAIL" in v.last_msg

    def test_demo_mode_apply_is_noop(self):
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.data = None
        v.confirm_action = "x"
        with patch.object(pd, "delete_pod") as dp:
            v.handle_key("y", app=MagicMock())
        dp.assert_not_called()
        assert v.last_ok is False
        assert "demo" in v.last_msg.lower()

    # --- double-tap (x+x / r+r / R+R) confirms without `y` -----------------

    def test_x_double_tap_applies(self):
        """Apertar `x` duas vezes confirma a ação (sem precisar do `y`)."""
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.handle_key("x", app=MagicMock())   # abre confirmação
        assert v.confirm_action == "x"
        with patch.object(pd, "delete_pod",
                          return_value=(True, "deleted")) as dp:
            v.handle_key("x", app=MagicMock())  # confirma via double-tap
        # PR #297: NS é propagado do RuntimeContext via kwarg.
        dp.assert_called_once_with("deile-worker-abc", namespace="deile")
        assert v.last_ok is True
        assert v.confirm_action is None

    def test_r_double_tap_applies(self):
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.handle_key("r", app=MagicMock())
        assert v.confirm_action == "r"
        with patch.object(pd, "rollout_restart_deployment",
                          return_value=(True, "restarted")) as rr:
            v.handle_key("r", app=MagicMock())
        rr.assert_called_once_with("deile-worker", namespace="deile")
        assert v.last_ok is True

    def test_R_double_tap_applies(self):
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.handle_key("R", app=MagicMock())
        assert v.confirm_action == "R"
        fake = [("deilebot", True, "ok"), ("deile-pipeline", True, "ok"),
                ("deile-shell", True, "ok"), ("deile-worker", True, "ok")]
        with patch.object(pd, "rollout_restart_all",
                          return_value=fake) as ra:
            v.handle_key("R", app=MagicMock())
        ra.assert_called_once_with(namespace="deile")
        assert v.last_ok is True

    def test_y_still_works_as_universal_confirm(self):
        """`y` continua funcionando — não quebra muscle-memory antigo."""
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.confirm_action = "x"
        with patch.object(pd, "delete_pod",
                          return_value=(True, "deleted")):
            v.handle_key("y", app=MagicMock())
        assert v.confirm_action is None
        assert v.last_ok is True

    def test_double_tap_does_not_cross_actions(self):
        """`x` na confirmação de `r` NÃO confirma — cancela como antes."""
        v = self._view_with_rows([_row("deile-worker-abc", "worker")])
        v.confirm_action = "r"   # pendente: restart
        with patch.object(pd, "rollout_restart_deployment") as rr:
            v.handle_key("x", app=MagicMock())  # tecla ERRADA → cancela
        rr.assert_not_called()
        assert v.confirm_action is None
        assert "cancelado" in v.last_msg


class TestGlobalKeyHandoffForPodPicker:
    """``r`` é global ("force refresh") em outras views, mas o PodPicker
    reivindica essa tecla pra rollout restart. ``_handle_global`` precisa
    ceder a tecla quando ``current_view.name == 'pod-picker'`` — esse teste
    pina o handoff para não regredir.
    """

    def _app_with_view(self, view_name: str):
        app = panel.PanelApp.__new__(panel.PanelApp)
        # `current_view` é property que retorna `stack[-1]`. Construímos
        # uma view stub com `.name` correto e empilhamos.
        view = MagicMock(spec=panel.View)
        view.name = view_name
        app.stack = [view]
        app.data = MagicMock()
        app._last_render = 100.0
        return app

    def test_r_ceded_to_pod_picker_view(self):
        app = self._app_with_view("pod-picker")
        assert app._handle_global("r") is False
        # Não força refresh nem invoca o cache — sinal claro de que cedeu.
        app.data.force_refresh_all.assert_not_called()
        assert app._last_render == 100.0

    def test_r_consumed_globally_for_other_views(self):
        app = self._app_with_view("dashboard")
        assert app._handle_global("r") is True
        app.data.force_refresh_all.assert_called_once()
        assert app._last_render == 0.0
