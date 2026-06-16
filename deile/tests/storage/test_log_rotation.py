"""Testes do ``HourlyDailyDirRotatingHandler``.

Cobertura:

1. **Namer**: traduz ``<base>.YYYY-MM-DD_HH`` em
   ``<logs_root>/<YYYY-MM-DD>/<HH>.log`` (e devolve o default em
   formatos inesperados).
2. **Rotator**: cria a subpasta diária e move o arquivo current;
   trata colisão fazendo append (sem perda de dados).
3. **GC**: subpastas mais antigas que ``retention_days`` são
   removidas; ``retention_days <= 0`` desliga o GC; entradas que não
   parecem subpastas diárias são ignoradas (não toca em
   ``security_audit.log`` nem em arquivos do operador).
4. **Inventário**: ``list_archived_log_files`` lista só os arquivos
   ``HH.log`` dentro de subpastas ``YYYY-MM-DD``, em ordem cronológica.
5. **End-to-end**: integração com logger real força um rollover e
   confirma que (a) o ``deile.log`` continua existindo, (b) o arquivo
   da hora anterior aterrissou na subpasta correta, (c) novas mensagens
   vão pro current.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from deile.storage.log_rotation import (
    HourlyDailyDirRotatingHandler,
    current_log_path,
    list_archived_log_files,
)

# ---------------------------------------------------------------------------
# namer
# ---------------------------------------------------------------------------


class TestNamer:
    def test_translates_default_name_to_daily_dir_layout(self, tmp_path):
        base = tmp_path / "deile.log"
        base.write_text("")
        h = HourlyDailyDirRotatingHandler(filename=str(base))
        try:
            default = f"{base}.2026-05-25_14"
            result = h.namer(default)
            assert result == str(tmp_path / "2026-05-25" / "14.log")
        finally:
            h.close()

    def test_returns_default_for_unexpected_format(self, tmp_path):
        base = tmp_path / "deile.log"
        base.write_text("")
        h = HourlyDailyDirRotatingHandler(filename=str(base))
        try:
            # Sem prefixo conhecido — devolve sem mexer.
            assert h.namer("/other/path") == "/other/path"
            # Prefixo OK mas suffix bagunçado:
            assert h.namer(str(base) + ".notadate") == str(base) + ".notadate"
            # Hora não-numérica:
            assert h.namer(str(base) + ".2026-05-25_XX") == str(base) + ".2026-05-25_XX"
        finally:
            h.close()


# ---------------------------------------------------------------------------
# rotator
# ---------------------------------------------------------------------------


class TestRotator:
    def test_creates_subdir_and_moves_source(self, tmp_path):
        base = tmp_path / "deile.log"
        base.write_text("hora atual\n")
        h = HourlyDailyDirRotatingHandler(filename=str(base))
        try:
            dest = str(tmp_path / "2026-05-25" / "14.log")
            h.rotator(str(base), dest)
            assert not base.exists()
            assert Path(dest).read_text() == "hora atual\n"
        finally:
            h.close()

    def test_collision_appends_to_existing(self, tmp_path):
        """Se o dest já existe (relógio voltou pra mesma hora), append
        em vez de sobrescrever — preserva dados."""
        base = tmp_path / "deile.log"
        base.write_text("nova hora\n")
        existing = tmp_path / "2026-05-25" / "14.log"
        existing.parent.mkdir(parents=True)
        existing.write_text("hora anterior já existente\n")
        h = HourlyDailyDirRotatingHandler(filename=str(base))
        try:
            h.rotator(str(base), str(existing))
            content = existing.read_text()
            assert "hora anterior já existente" in content
            assert "nova hora" in content
            # Source foi consumido:
            assert not base.exists()
        finally:
            h.close()

    def test_oserror_during_replace_leaves_source_intact(self, tmp_path, monkeypatch):
        """Se o OS bloquear o rename (FS read-only, permissões), o source
        deve sobreviver — TimedRotatingFileHandler vai reabrir depois.
        """
        base = tmp_path / "deile.log"
        base.write_text("conteúdo importante\n")
        h = HourlyDailyDirRotatingHandler(filename=str(base))
        try:

            def _raise(*_a, **_kw):
                raise OSError("disk full")

            monkeypatch.setattr(os, "replace", _raise)
            h.rotator(str(base), str(tmp_path / "2026-05-25" / "14.log"))
            # Sobreviveu:
            assert base.exists()
            assert "conteúdo importante" in base.read_text()
        finally:
            h.close()


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------


class TestGC:
    def _seed_daily_dir(self, root: Path, date_str: str) -> Path:
        d = root / date_str
        d.mkdir(parents=True, exist_ok=True)
        (d / "00.log").write_text("x")
        # Mexer mtime no dir não é confiável cross-platform; o GC usa o
        # NOME da pasta (que é a data), não o mtime — daí o teste só
        # precisa preencher conteúdo e checar a remoção por nome.
        return d

    def test_purges_dirs_older_than_retention(self, tmp_path):
        old_date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        recent_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        old_dir = self._seed_daily_dir(tmp_path, old_date)
        recent_dir = self._seed_daily_dir(tmp_path, recent_date)
        h = HourlyDailyDirRotatingHandler(
            filename=str(tmp_path / "deile.log"),
            retention_days=30,
        )
        try:
            h._purge_old_dirs()  # noqa: SLF001 — testando helper interno
        finally:
            h.close()
        assert not old_dir.exists()
        assert recent_dir.exists()

    def test_retention_zero_disables_gc(self, tmp_path):
        old_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        old_dir = self._seed_daily_dir(tmp_path, old_date)
        h = HourlyDailyDirRotatingHandler(
            filename=str(tmp_path / "deile.log"),
            retention_days=0,
        )
        try:
            h._purge_old_dirs()  # noqa: SLF001
        finally:
            h.close()
        assert old_dir.exists()

    def test_ignores_non_daily_entries(self, tmp_path):
        """GC NÃO toca em security_audit.log ou arquivos do operador."""
        (tmp_path / "security_audit.log").write_text("audit")
        operator_file = tmp_path / "operator-notes.md"
        operator_file.write_text("# importante")
        bogus_dir = tmp_path / "not-a-date"
        bogus_dir.mkdir()
        (bogus_dir / "stuff").write_text("x")
        old_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        old_dir = self._seed_daily_dir(tmp_path, old_date)
        h = HourlyDailyDirRotatingHandler(
            filename=str(tmp_path / "deile.log"),
            retention_days=30,
        )
        try:
            h._purge_old_dirs()  # noqa: SLF001
        finally:
            h.close()
        # Old daily dir foi removido.
        assert not old_dir.exists()
        # Resto sobrevive.
        assert (tmp_path / "security_audit.log").exists()
        assert operator_file.exists()
        assert bogus_dir.exists()
        assert (bogus_dir / "stuff").exists()


# ---------------------------------------------------------------------------
# inventário
# ---------------------------------------------------------------------------


class TestInventory:
    def test_lists_only_hourly_files_in_chrono_order(self, tmp_path):
        # Mistura: subpastas válidas + arquivos soltos + subpasta inválida
        (tmp_path / "2026-05-25").mkdir()
        (tmp_path / "2026-05-25" / "14.log").write_text("a")
        (tmp_path / "2026-05-25" / "02.log").write_text("b")
        (tmp_path / "2026-05-26").mkdir()
        (tmp_path / "2026-05-26" / "00.log").write_text("c")
        (tmp_path / "2026-05-26" / "notes.txt").write_text("ignorar")
        (tmp_path / "deile.log").write_text("current — não deve aparecer")
        (tmp_path / "not-a-date").mkdir()
        (tmp_path / "not-a-date" / "99.log").write_text("ignorar")

        archived = list_archived_log_files(tmp_path)
        names = [(p.parent.name, p.name) for p in archived]
        assert names == [
            ("2026-05-25", "02.log"),
            ("2026-05-25", "14.log"),
            ("2026-05-26", "00.log"),
        ]

    def test_returns_empty_when_root_missing(self, tmp_path):
        assert list_archived_log_files(tmp_path / "nope") == []

    def test_current_log_path_is_stable(self, tmp_path):
        assert current_log_path(tmp_path) == tmp_path / "deile.log"


# ---------------------------------------------------------------------------
# end-to-end com logger real (forçando um rollover)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_force_rollover_archives_current_and_keeps_writing(self, tmp_path):
        base = tmp_path / "deile.log"
        h = HourlyDailyDirRotatingHandler(filename=str(base))
        # Escreve algo, força rollover, escreve mais.
        logger = logging.getLogger("test.log_rotation.e2e")
        logger.handlers.clear()
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            logger.info("primeira mensagem")
            # Força rollover sem esperar o relógio — `doRollover()`
            # honra `self.rolloverAt`, então setamos pro passado.
            h.rolloverAt = int(time.time()) - 1
            h.doRollover()
            logger.info("segunda mensagem")
            # 1) deile.log existe + tem a segunda mensagem.
            assert base.exists()
            assert "segunda mensagem" in base.read_text()
            # 2) Existe UMA subpasta diária com UM arquivo HH.log
            #    contendo a primeira mensagem.
            archived = list_archived_log_files(tmp_path)
            assert len(archived) == 1
            assert "primeira mensagem" in archived[0].read_text()
            # 3) O nome do arquivo arquivado segue o padrão HH.log.
            assert archived[0].name.endswith(".log")
            assert archived[0].name.split(".")[0].isdigit()
        finally:
            logger.removeHandler(h)
            h.close()


# ---------------------------------------------------------------------------
# pytest guard em logs._ensure_initialized
# ---------------------------------------------------------------------------


class TestPytestGuard:
    def test_pytest_run_installs_null_handler_not_filehandler(self):
        """Regressão: durante pytest, get_logger NÃO deve criar
        ``~/.deile/logs/deile.log`` (estamos rodando dentro de pytest;
        o guard tem que disparar)."""
        from deile.storage import logs as logs_mod

        # Reset do estado pra forçar reinicialização sob pytest:
        logs_mod._initialized = False
        deile_logger = logging.getLogger("deile")
        # Limpa handlers anteriores para forçar reinit:
        saved = deile_logger.handlers[:]
        deile_logger.handlers.clear()
        try:
            lg = logs_mod.get_logger()
            assert lg.handlers, "get_logger deve instalar pelo menos 1 handler"
            assert any(isinstance(h, logging.NullHandler) for h in lg.handlers)
            # NÃO deve haver FileHandler nem subclass (incl. nosso rotator).
            from deile.storage.log_rotation import HourlyDailyDirRotatingHandler

            assert not any(
                isinstance(h, (logging.FileHandler, HourlyDailyDirRotatingHandler))
                for h in lg.handlers
            )
        finally:
            # Restaura estado pra não afetar outros testes.
            deile_logger.handlers.clear()
            for h in saved:
                deile_logger.addHandler(h)
            logs_mod._initialized = True
