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

Importante: nada bate em editor real — todos os ``subprocess.Popen``
são mockados. Os tempfiles criados pelo dump são limpos no teardown.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402


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
