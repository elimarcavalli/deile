"""Testes do hotkey `[.] abrir log` em ``PodWatchView``.

Cobertura:

1. ``_open_path_in_editor`` — preferência cursor > code > fallback
   por plataforma (Windows/macOS/Linux); ``(False, "")`` quando nada
   está disponível.
2. ``PodWatchView._resolve_log_path_for_editor`` — local devolve
   ``logs_dir/deile.log``; k8s dumpa o buffer do streamer num tempfile
   estável por pod.
3. ``PodWatchView.handle_key('.')`` — dispara o opener e popula
   ``_status_msg`` (sucesso e falha); auto-limpa após TTL.
4. ``PodWatchView._header_body()`` — linha "current task" mostra o
   issue/PR/workflow que o worker está atendendo agora (issue #309
   fase 2 follow-up). Fonte de verdade reutilizada: o
   :class:`CurrentTask` populado pelo :class:`WorkerProvider` a partir
   da linha estruturada ``dispatch_started`` emitida pelo worker.

Importante: nada bate em editor real — todos os ``subprocess.Popen``
são mockados. Os tempfiles criados pelo dump são limpos no teardown.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from rich.console import Console

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

# ---------------------------------------------------------------------------
# _open_path_in_editor — ordem de preferência e fallbacks
# ---------------------------------------------------------------------------

class TestOpenPathInEditor:
    def _which_factory(self, available):
        """Helper: devolve uma função `which(name)` que só conhece
        os binários listados em `available` (dict name -> resolved_path)."""
        def _which(name):
            return available.get(name)
        return _which

    def test_prefers_cursor_when_available(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_text("hello")
        avail = {"cursor": "/usr/local/bin/cursor", "code": "/usr/local/bin/code"}
        with patch.object(panel.shutil, "which",
                          side_effect=self._which_factory(avail)), \
             patch.object(panel.subprocess, "Popen") as popen:
            ok, tool = panel._open_path_in_editor(f)
        assert ok is True
        assert tool == "cursor"
        assert popen.call_args[0][0][0] == "/usr/local/bin/cursor"

    def test_falls_back_to_code_when_cursor_missing(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_text("hello")
        avail = {"code": "/usr/local/bin/code"}
        with patch.object(panel.shutil, "which",
                          side_effect=self._which_factory(avail)), \
             patch.object(panel.subprocess, "Popen") as popen:
            ok, tool = panel._open_path_in_editor(f)
        assert ok is True
        assert tool == "code"
        assert popen.call_args[0][0][0] == "/usr/local/bin/code"

    def test_macos_open_t_fallback(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_text("hello")
        avail = {"open": "/usr/bin/open"}
        with patch.object(panel.shutil, "which",
                          side_effect=self._which_factory(avail)), \
             patch.object(panel, "sys", MagicMock(platform="darwin")), \
             patch.object(panel.os, "name", "posix"), \
             patch.object(panel.subprocess, "Popen") as popen:
            ok, tool = panel._open_path_in_editor(f)
        assert ok is True
        assert tool == "open -t"
        # `open -t` precisa do flag `-t` na posição 1.
        cmd = popen.call_args[0][0]
        assert cmd[0] == "/usr/bin/open"
        assert cmd[1] == "-t"

    def test_linux_xdg_open_fallback(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_text("hello")
        avail = {"xdg-open": "/usr/bin/xdg-open"}
        with patch.object(panel.shutil, "which",
                          side_effect=self._which_factory(avail)), \
             patch.object(panel, "sys", MagicMock(platform="linux")), \
             patch.object(panel.os, "name", "posix"), \
             patch.object(panel.subprocess, "Popen") as popen:
            ok, tool = panel._open_path_in_editor(f)
        assert ok is True
        assert tool == "xdg-open"
        assert popen.call_args[0][0][0] == "/usr/bin/xdg-open"

    def test_windows_notepad_fallback(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_text("hello")
        # No Windows: cursor/code ausentes, notepad sempre disponível
        # sem precisar passar por `which`.
        avail = {}
        with patch.object(panel.shutil, "which",
                          side_effect=self._which_factory(avail)), \
             patch.object(panel.os, "name", "nt"), \
             patch.object(panel.subprocess, "Popen") as popen:
            ok, tool = panel._open_path_in_editor(f)
        assert ok is True
        assert tool == "notepad"
        assert popen.call_args[0][0][0] == "notepad"

    def test_returns_false_when_nothing_available(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_text("hello")
        avail = {}  # nada disponível
        with patch.object(panel.shutil, "which",
                          side_effect=self._which_factory(avail)), \
             patch.object(panel, "sys", MagicMock(platform="linux")), \
             patch.object(panel.os, "name", "posix"), \
             patch.object(panel.subprocess, "Popen") as popen:
            ok, tool = panel._open_path_in_editor(f)
        assert ok is False
        assert tool == ""
        popen.assert_not_called()

    def test_oserror_in_first_editor_skips_to_next(self, tmp_path):
        """Se `cursor` está no PATH mas Popen levanta OSError (ex: binário
        quebrado), tentamos `code` antes de cair pra plataforma."""
        f = tmp_path / "x.log"
        f.write_text("hello")
        avail = {"cursor": "/usr/local/bin/cursor",
                 "code": "/usr/local/bin/code"}
        # Popen falha pro cursor, sucesso pro code.
        with patch.object(panel.shutil, "which",
                          side_effect=self._which_factory(avail)), \
             patch.object(panel.subprocess, "Popen",
                          side_effect=[OSError("boom"), MagicMock()]) as popen:
            ok, tool = panel._open_path_in_editor(f)
        assert ok is True
        assert tool == "code"
        assert popen.call_count == 2


# ---------------------------------------------------------------------------
# PodWatchView._resolve_log_path_for_editor — local vs k8s
# ---------------------------------------------------------------------------

class TestResolveLogPath:
    def _make_view_local(self, tmp_path):
        view = panel.PodWatchView(data=MagicMock())
        view.pod_role = "local-worker"
        view.pod_name = "DEILE@/home/user (12345)"
        view.data.context.logs_dir = tmp_path
        return view

    def _make_view_k8s(self):
        view = panel.PodWatchView(data=MagicMock())
        view.pod_role = "worker"
        view.pod_name = "deile-worker-7d49f9544b-5bwjk"
        return view

    def test_local_role_returns_logs_dir_deile_log(self, tmp_path):
        view = self._make_view_local(tmp_path)
        path = view._resolve_log_path_for_editor()
        assert path == tmp_path / "deile.log"

    def test_local_role_returns_none_when_data_missing(self, tmp_path):
        view = panel.PodWatchView(data=None)
        view.pod_role = "local-worker"
        view.pod_name = "x"
        assert view._resolve_log_path_for_editor() is None

    def test_k8s_role_dumps_buffer_to_tempfile(self):
        view = self._make_view_k8s()
        view.streamer = MagicMock()
        view.streamer.snapshot.return_value = [
            "linha 1",
            "linha 2 com acento: ção",
            "linha 3",
        ]
        path = view._resolve_log_path_for_editor()
        try:
            assert path is not None
            assert path.exists()
            content = path.read_text(encoding="utf-8")
            assert "linha 1" in content
            assert "ção" in content
            assert "deile-worker-7d49f9544b-5bwjk" in content  # header
        finally:
            if path is not None and path.exists():
                path.unlink()

    def test_k8s_role_uses_stable_name_per_pod(self):
        view = self._make_view_k8s()
        view.streamer = MagicMock()
        view.streamer.snapshot.return_value = ["x"]
        p1 = view._resolve_log_path_for_editor()
        view.streamer.snapshot.return_value = ["y", "z"]
        p2 = view._resolve_log_path_for_editor()
        try:
            assert p1 == p2  # mesmo pod → mesmo arquivo
            # E o segundo dump sobrescreveu, não fez append:
            assert "z" in p2.read_text(encoding="utf-8")
        finally:
            if p1 is not None and p1.exists():
                p1.unlink()

    def test_k8s_role_returns_none_when_streamer_none(self):
        view = self._make_view_k8s()
        view.streamer = None
        assert view._resolve_log_path_for_editor() is None

    def test_k8s_role_sanitizes_pod_name_for_filename(self):
        """Pod name vem do cluster mas é defesa em profundidade: o
        sanitizador deve garantir que o resultado seja UM componente
        de path (sem separadores) e que o arquivo final permaneça
        dentro de ``tempfile.gettempdir()``."""
        import tempfile as _tempfile
        view = self._make_view_k8s()
        view.pod_name = "../../etc/passwd"  # tentativa de path traversal
        view.streamer = MagicMock()
        view.streamer.snapshot.return_value = ["x"]
        path = view._resolve_log_path_for_editor()
        try:
            assert path is not None
            # Nenhum separador de path no nome do arquivo final.
            assert os.sep not in path.name
            assert "/" not in path.name
            # Path resolvido fica DENTRO do tempdir (sem escape via `..`).
            tmpdir = Path(_tempfile.gettempdir()).resolve()
            assert path.resolve().parent == tmpdir
            # Mantém o prefixo previsível:
            assert path.name.startswith("deile-podwatch-")
            assert path.name.endswith(".log")
        finally:
            if path is not None and path.exists():
                path.unlink()


# ---------------------------------------------------------------------------
# PodWatchView.handle_key('.') — fluxo completo
# ---------------------------------------------------------------------------

class TestHandleKeyDot:
    def _view_with_local_log(self, tmp_path):
        view = panel.PodWatchView(data=MagicMock())
        view.pod_role = "local-worker"
        view.pod_name = "x"
        view.data.context.logs_dir = tmp_path
        # Cria o log file (senão handler reporta "arquivo não existe").
        (tmp_path / "deile.log").write_text("conteúdo")
        return view

    def test_dot_key_success_sets_status(self, tmp_path):
        view = self._view_with_local_log(tmp_path)
        with patch.object(panel, "_open_path_in_editor",
                          return_value=(True, "cursor")) as opener:
            result = view.handle_key(".", MagicMock())
        assert result.kind == panel.Action.REFRESH
        assert view._status_msg is not None
        assert "cursor" in view._status_msg
        assert "deile.log" in view._status_msg
        opener.assert_called_once()

    def test_dot_key_failure_sets_status(self, tmp_path):
        view = self._view_with_local_log(tmp_path)
        with patch.object(panel, "_open_path_in_editor",
                          return_value=(False, "")):
            view.handle_key(".", MagicMock())
        assert view._status_msg is not None
        assert "nenhum editor" in view._status_msg.lower()

    def test_dot_key_missing_file_sets_status_no_opener_call(self, tmp_path):
        view = panel.PodWatchView(data=MagicMock())
        view.pod_role = "local-worker"
        view.pod_name = "x"
        view.data.context.logs_dir = tmp_path  # sem criar o deile.log
        with patch.object(panel, "_open_path_in_editor") as opener:
            view.handle_key(".", MagicMock())
        assert view._status_msg is not None
        assert "não existe" in view._status_msg
        opener.assert_not_called()

    def test_dot_key_unresolvable_path_sets_status(self):
        view = panel.PodWatchView(data=None)  # sem context
        view.pod_role = "local-worker"
        view.pod_name = "x"
        with patch.object(panel, "_open_path_in_editor") as opener:
            view.handle_key(".", MagicMock())
        assert view._status_msg is not None
        assert "não consegui resolver" in view._status_msg
        opener.assert_not_called()

    def test_status_auto_clears_after_ttl(self, tmp_path):
        view = self._view_with_local_log(tmp_path)
        with patch.object(panel, "_open_path_in_editor",
                          return_value=(True, "cursor")):
            view.handle_key(".", MagicMock())
        # Status presente imediatamente após o key press.
        assert view._status_msg is not None
        # Simula passagem de tempo > TTL.
        view._status_until = time.time() - 1.0
        # `_log_panel` é o lugar que limpa o status expirado no render.
        view.streamer = None  # evita exigir _LogStreamer real
        view._log_panel()
        assert view._status_msg is None

    def test_hotkeys_footer_mentions_dot(self):
        """Regressão: ninguém remova `.` do footer sem ajustar a help."""
        assert "[.] abrir log" in panel.PodWatchView.HOTKEYS


class TestPodWatchTmpResize:
    """Hotkey [t] → presets → kubectl patch via set_pod_tmp_size."""

    def _view(self, role: str = "worker") -> "panel.PodWatchView":
        view = panel.PodWatchView(data=MagicMock())
        view.pod_role = role
        view.pod_name = "deile-worker-abc"
        view.data.context.namespace = "deile"
        return view

    def test_hotkeys_footer_mentions_t(self):
        """Regressão: ninguém remova [t] do footer sem ajustar a help."""
        assert "[t] resize /tmp" in panel.PodWatchView.HOTKEYS

    def test_t_key_enters_preset_mode_for_k8s_pod(self):
        view = self._view("worker")
        result = view.handle_key("t", MagicMock())
        assert result.kind == panel.Action.REFRESH
        assert view._awaiting_tmp_preset is True
        assert view._status_msg is not None
        assert "/tmp resize" in view._status_msg

    def test_t_key_rejected_for_local_role(self):
        view = self._view("local-pipeline")
        view.handle_key("t", MagicMock())
        assert view._awaiting_tmp_preset is False
        assert view._status_msg is not None
        assert "processo local" in view._status_msg

    def test_t_key_rejected_for_unmapped_role(self):
        view = self._view("unknown-role")
        view.handle_key("t", MagicMock())
        assert view._awaiting_tmp_preset is False
        assert view._status_msg is not None
        assert "Deployment associado" in view._status_msg

    def test_preset_key_calls_set_pod_tmp_size(self):
        view = self._view("worker")
        view.handle_key("t", MagicMock())  # entra no modo
        assert view._awaiting_tmp_preset is True
        with patch.object(pd, "set_pod_tmp_size",
                          return_value=(True, "deployment patched")) as setter:
            view.handle_key("4", MagicMock())  # preset 4 = 2Gi
        assert view._awaiting_tmp_preset is False
        setter.assert_called_once_with("deile-worker", "2Gi", namespace="deile")
        assert "OK" in view._status_msg

    def test_preset_uses_role_to_deployment_mapping(self):
        for role, dep in (
            ("pipeline", "deile-pipeline"),
            ("worker",   "deile-worker"),
            ("bot",      "deilebot"),
            ("shell",    "deile-shell"),
        ):
            view = self._view(role)
            view.handle_key("t", MagicMock())
            with patch.object(pd, "set_pod_tmp_size",
                              return_value=(True, "ok")) as setter:
                view.handle_key("3", MagicMock())  # 1Gi
            assert setter.call_args.args[0] == dep

    def test_non_preset_key_cancels_silently(self):
        view = self._view("worker")
        view.handle_key("t", MagicMock())
        with patch.object(pd, "set_pod_tmp_size") as setter:
            view.handle_key("z", MagicMock())  # tecla não-mapeada
        setter.assert_not_called()
        assert view._awaiting_tmp_preset is False
        assert "cancelado" in view._status_msg

    def test_other_hotkeys_blocked_while_awaiting_preset(self):
        """Modo preset deve consumir a próxima tecla — `f` (follow) não pode
        disparar enquanto o modal estiver ativo."""
        view = self._view("worker")
        view.handle_key("t", MagicMock())
        following_before = view.following
        with patch.object(pd, "set_pod_tmp_size") as setter:
            view.handle_key("f", MagicMock())  # `f` no modo preset = cancela
        setter.assert_not_called()
        assert view._awaiting_tmp_preset is False
        # following NÃO foi togglado (a tecla virou cancel do modo).
        assert view.following == following_before

    def test_setter_failure_surfaces_in_status(self):
        view = self._view("worker")
        view.handle_key("t", MagicMock())
        with patch.object(pd, "set_pod_tmp_size",
                          return_value=(False, "kubectl error: boom")):
            view.handle_key("3", MagicMock())
        assert "FAIL" in view._status_msg
        assert "boom" in view._status_msg


# ---------------------------------------------------------------------------
# Memdebug (--memdebug) — off por default, opcional via flag
# ---------------------------------------------------------------------------

class TestMemdebug:
    def test_off_by_default_returns_empty_string(self):
        """`memdebug=False` (default): nunca instancia tracemalloc, sempre
        devolve `""`. Garante zero overhead em uso normal."""
        app = panel.PanelApp(views={"dashboard": panel.HelpView()})
        assert app._memdebug is False
        assert app.memdebug_line() == ""

    def test_on_returns_mem_line_after_first_sample(self):
        """`memdebug=True`: o primeiro call faz snapshot e devolve uma
        string `mem: cur ... peak ...`. Calls subsequentes dentro do
        intervalo devolvem o cache, não re-amostram."""
        app = panel.PanelApp(views={"dashboard": panel.HelpView()},
                             memdebug=True)
        line = app.memdebug_line()
        assert line.startswith("mem: cur ")
        assert "peak" in line
        # Segunda chamada imediata devolve o mesmo cache (sem re-sample):
        assert app.memdebug_line() == line

    def test_on_records_delta_between_samples(self):
        """Quando o intervalo passa, o segundo sample inclui `Δ60s`."""
        app = panel.PanelApp(views={"dashboard": panel.HelpView()},
                             memdebug=True)
        app.memdebug_line()  # primeiro sample (sem delta)
        # Força o "intervalo" passar:
        app._memdebug_last_sample_at = 0.0
        line2 = app.memdebug_line()
        assert "Δ" in line2  # tem o delta agora


# ---------------------------------------------------------------------------
# CurrentTask + WorkerProvider — issue #309 fase 2 follow-up
# ---------------------------------------------------------------------------

def _ts_now_str(offset_s: int = 0) -> str:
    """Format helper para timestamps no formato ``kubectl logs --timestamps``."""
    from datetime import timedelta
    ts = datetime.now(timezone.utc) - timedelta(seconds=offset_s)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000000000+00:00")


class TestCurrentTaskTargetLabel:
    """``CurrentTask.target_label`` deve renderizar o rótulo de forma
    forge-agnóstica e tolerar callers sem ``issue_number`` explícito —
    nesse caso, extrai do ``channel_id`` no padrão
    ``pipeline-(issue|pr|mention-issue|mention-pr)-<N>``.
    """

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def test_explicit_issue_number_wins(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-issue-309",
            started_ts=self._now(), issue_number=309,
        )
        assert ct.target_label == "#309"

    def test_pr_channel_renders_pr_prefix(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-pr-291",
            started_ts=self._now(),
        )
        assert ct.target_label == "PR#291"

    def test_issue_extracted_from_pipeline_channel_when_missing(self):
        """Backward compat: pipeline antigo (sem ``issue_number`` no
        payload) ainda renderiza corretamente extraindo do channel_id."""
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-issue-309",
            started_ts=self._now(),
        )
        assert ct.target_label == "#309"

    def test_mention_issue_channel(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-mention-issue-257",
            started_ts=self._now(),
        )
        assert "mention" in ct.target_label
        assert "257" in ct.target_label

    def test_mention_pr_channel(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-mention-pr-261",
            started_ts=self._now(),
        )
        assert "mention" in ct.target_label
        assert "261" in ct.target_label

    def test_non_pipeline_channel_falls_back_to_channel_id(self):
        """Dispatches do bot/CLI usam o snowflake do Discord — sem padrão
        pipeline-* extraível, mostramos o channel_id truncado pra dar
        algum contexto ao operador."""
        ct = pd.CurrentTask(
            task_id="abc", channel_id="1234567890123456789",
            started_ts=self._now(),
        )
        assert ct.target_label.startswith("channel:")

    def test_elapsed_is_non_negative(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="x",
            started_ts=datetime.now(timezone.utc),
        )
        assert ct.elapsed_s >= 0


class TestWorkerProviderCurrentTask:
    """``WorkerProvider._parse`` extrai ``current_task`` da linha
    estruturada ``dispatch_started`` emitida pelo worker server e
    encerra quando vê o ``dispatch_completed`` pareado."""

    def _build(self) -> "pd.WorkerProvider":
        prov = pd.WorkerProvider(ttl_s=0.0)
        prov._kubectl = "kubectl"
        return prov

    def test_current_task_extracted_from_dispatch_started(self):
        prov = self._build()
        body = ("dispatch_started task=abc123def456 channel=pipeline-issue-309 "
                "stage=implement kind=implement issue=309 branch=auto/issue-309")
        text = f"{_ts_now_str(2)} {body}"
        state = prov._parse("worker-1", text)
        assert state.current_task is not None
        assert state.current_task.task_id == "abc123def456"
        assert state.current_task.channel_id == "pipeline-issue-309"
        assert state.current_task.stage == "implement"
        assert state.current_task.action_kind == "implement"
        assert state.current_task.issue_number == 309
        assert state.current_task.branch == "auto/issue-309"
        assert state.current_task.target_label == "#309"

    def test_current_task_cleared_after_dispatch_completed(self):
        prov = self._build()
        text = "\n".join([
            f"{_ts_now_str(5)} dispatch_started task=abc123def456 "
            f"channel=pipeline-issue-309 stage=implement issue=309",
            f"{_ts_now_str(2)} dispatch_completed task=abc123def456 ok=True",
        ])
        state = prov._parse("worker-1", text)
        # Pareamento started+completed → current_task = None (idle).
        assert state.current_task is None

    def test_current_task_is_latest_unmatched_start(self):
        """Quando há múltiplas dispatches sobrepostas (raro, mas possível
        em workers concurrent), current_task = a started mais recente
        ainda sem completed pareado."""
        prov = self._build()
        text = "\n".join([
            f"{_ts_now_str(10)} dispatch_started task=oldoldoldold1 "
            f"channel=pipeline-issue-100 issue=100",
            f"{_ts_now_str(8)} dispatch_completed task=oldoldoldold1 ok=True",
            f"{_ts_now_str(5)} dispatch_started task=newnewnewnew1 "
            f"channel=pipeline-issue-200 issue=200",
        ])
        state = prov._parse("worker-1", text)
        assert state.current_task is not None
        assert state.current_task.issue_number == 200
        assert state.current_task.task_id == "newnewnewnew1"

    def test_current_task_none_when_log_has_only_health(self):
        prov = self._build()
        text = (
            f'{_ts_now_str(2)} aiohttp.access "GET /v1/health HTTP/1.1" 200 237'
        )
        state = prov._parse("worker-idle", text)
        assert state.current_task is None

    def test_current_task_survives_old_pipeline_without_issue_field(self):
        """Worker antigo só logava ``channel`` — devemos extrair issue
        do channel_id pra UI continuar útil."""
        prov = self._build()
        body = "dispatch_started task=abc123def456 channel=pipeline-issue-309"
        text = f"{_ts_now_str(2)} {body}"
        state = prov._parse("worker-1", text)
        assert state.current_task is not None
        # issue_number explícito ausente, mas target_label deriva do channel
        assert state.current_task.issue_number is None
        assert state.current_task.target_label == "#309"

    def test_dispatch_started_does_not_double_count_substantive(self):
        """``dispatch_started`` deve atualizar ``last_substantive_ts`` mas
        não criar um second ``last_substantive_body`` separado da access
        log normal — só queremos um ponto de "atividade real" por dispatch.
        """
        prov = self._build()
        body = "dispatch_started task=abc123def456 channel=pipeline-issue-309 issue=309"
        text = f"{_ts_now_str(2)} {body}"
        state = prov._parse("worker-1", text)
        # last_substantive_ts atualizado pela linha started.
        assert state.last_substantive_ts is not None


class TestPodWatchViewCurrentTask:
    """``PodWatchView._header_body`` deve incluir uma linha "current task:"
    com o rótulo do CurrentTask quando o worker está BUSY, e "— (idle)"
    quando current_task=None. Pods não-worker NÃO ganham essa linha."""

    def _setup_view_with_pod(self, *, current_task=None, busy: bool = False):
        view = panel.PodWatchView(data=MagicMock())
        view.pod_role = "worker"
        view.pod_name = "deile-worker-7d49f9544b-5bwjk"
        # Mock PodInfo minimal — _header_body usa name/role/status/age_s/
        # restarts/ready/node.
        pod = MagicMock()
        pod.name = view.pod_name
        pod.role = "worker"
        pod.status = "Running"
        pod.age_s = 120.0
        pod.restarts = 0
        pod.ready = True
        pod.node = "worker-node-1"
        view.data.pods.get.return_value = [pod]
        # WorkerState com current_task.
        wstate = pd.WorkerState(pod_name=view.pod_name, busy=busy,
                                current_task=current_task)
        view.data.workers.get.return_value = {view.pod_name: wstate}
        return view

    def _render_to_text(self, view: "panel.PodWatchView") -> str:
        """Captura o output Rich do header como string plana pra asserts."""
        renderable = view._header_body()
        console = Console(record=True, width=200, force_terminal=False,
                          color_system=None)
        console.print(renderable)
        return console.export_text()

    def test_busy_worker_with_issue_shows_current_task_line(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-issue-309",
            started_ts=datetime.now(timezone.utc), stage="implement",
            issue_number=309, branch="auto/issue-309",
        )
        view = self._setup_view_with_pod(current_task=ct, busy=True)
        out = self._render_to_text(view)
        assert "current task:" in out
        assert "#309" in out
        assert "implement" in out  # stage
        assert "auto/issue-309" in out  # branch

    def test_busy_worker_with_pr_shows_pr_prefix(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-pr-291",
            started_ts=datetime.now(timezone.utc), stage="pr_review",
        )
        view = self._setup_view_with_pod(current_task=ct, busy=True)
        out = self._render_to_text(view)
        assert "PR#291" in out
        assert "pr_review" in out

    def test_idle_worker_shows_dash(self):
        view = self._setup_view_with_pod(current_task=None, busy=False)
        out = self._render_to_text(view)
        assert "current task:" in out
        # Estado idle — não menciona um número de issue/PR
        assert "idle" in out.lower()

    def test_non_worker_pod_has_no_current_task_line(self):
        """Pods que não são worker (bot, pipeline, shell) não tem
        ``wstate`` no dict de workers — a linha "current task:" NÃO
        aparece (mantém compat visual com pipeline/bot/shell)."""
        view = panel.PodWatchView(data=MagicMock())
        view.pod_role = "pipeline"  # não-worker
        view.pod_name = "deile-pipeline-xyz"
        pod = MagicMock()
        pod.name = view.pod_name
        pod.role = "pipeline"
        pod.status = "Running"
        pod.age_s = 600.0
        pod.restarts = 0
        pod.ready = True
        pod.node = "n1"
        view.data.pods.get.return_value = [pod]
        # workers.get() devolve {} (sem este pod).
        view.data.workers.get.return_value = {}
        out = self._render_to_text(view)
        assert "current task:" not in out

    def test_target_label_is_forge_agnostic(self):
        """O renderer não distingue GitHub de GitLab — usa o vocabulário
        unificado ``#N`` / ``PR#N`` / ``mention …`` (Decisão #42).
        Forge-specific PR↔MR é abstraído upstream na camada forge."""
        # Mesma fixture pra dois forge "kinds" — o output é idêntico.
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-issue-309",
            started_ts=datetime.now(timezone.utc), issue_number=309,
        )
        view = self._setup_view_with_pod(current_task=ct, busy=True)
        out = self._render_to_text(view)
        # Não deve aparecer terminologia GitLab nem GitHub específica.
        assert "!309" not in out  # GitLab MR prefix
        assert "MR" not in out    # GitLab MR vocab
        assert "#309" in out      # vocabulário unificado


class TestPodWatchViewRenderSize:
    """Layout deve acomodar 4 linhas + bordas sem cortar a linha de
    current task — regression guard pro size=7 do split_column."""

    def test_render_includes_current_task_section(self):
        ct = pd.CurrentTask(
            task_id="abc", channel_id="pipeline-issue-309",
            started_ts=datetime.now(timezone.utc), issue_number=309,
        )
        view = panel.PodWatchView(data=MagicMock())
        view.pod_role = "worker"
        view.pod_name = "deile-worker-xyz"
        pod = MagicMock()
        pod.name = view.pod_name
        pod.role = "worker"
        pod.status = "Running"
        pod.age_s = 60.0
        pod.restarts = 0
        pod.ready = True
        pod.node = "n1"
        view.data.pods.get.return_value = [pod]
        view.data.workers.get.return_value = {
            view.pod_name: pd.WorkerState(pod_name=view.pod_name,
                                          busy=True, current_task=ct),
        }
        app = MagicMock()
        app.pause = False  # _head_panel pode consultar
        # Mock _head_panel direto pra não depender do PanelApp completo.
        with patch.object(panel, "_head_panel",
                          return_value=panel.Text("HEAD")):
            with patch.object(panel, "_footer_panel",
                              return_value=panel.Text("FOOTER")):
                layout = view.render(app)
        # Layout deve renderizar sem exceções.
        console = Console(record=True, width=200, force_terminal=False,
                          color_system=None, height=40)
        console.print(layout)
        out = console.export_text()
        assert "#309" in out
        assert "current task:" in out
