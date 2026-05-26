"""Hourly + daily-folder rotating file handler para `deile.log`.

Layout em disco:

    ~/.deile/logs/
        deile.log                  <- current hour (sempre existe; o que
                                       leitores `tail -F` seguem)
        2026-05-25/
            00.log                 <- horas anteriores arquivadas
            01.log
            ...
            23.log
        2026-05-26/
            00.log
            ...

Por que ``deile.log`` continua no raiz:

- Compatibilidade: todos os leitores existentes apontam para
  `<logs_dir>/deile.log` (painel TUI ``_LocalLogTailer``,
  ``LocalLogsProvider``, ações ``[local] tail deile.log``,
  ``PodWatchView._resolve_log_path_for_editor``). Mantendo o nome,
  nenhum leitor precisa mudar.
- ``tail -F deile.log`` (que o painel usa) re-abre o arquivo por
  filename após o rename — então ele segue a "nova" current hour
  automaticamente quando rotacionamos.

Como funciona o rollover:

1. ``shouldRollover()`` (herdado de :class:`TimedRotatingFileHandler`)
   dispara na virada da hora local (``when='H', interval=1``).
2. ``doRollover()`` (herdado) chama ``self.rotate(base, dfn)``, onde
   ``dfn`` é computado por ``self.rotation_filename(...)``.
3. ``self.namer`` (definido abaixo) reescreve o ``dfn`` padrão
   (``deile.log.YYYY-MM-DD_HH``) para
   ``<logs_dir>/<YYYY-MM-DD>/<HH>.log``.
4. ``self.rotator`` (definido abaixo) cria a subpasta diária com
   ``mkdir(parents=True, exist_ok=True)`` ANTES do ``os.replace`` — sem
   isso o rename quebra na primeira virada de hora do dia.
5. GC silencioso (``_purge_old_dirs``) remove subpastas diárias mais
   velhas que ``retention_days``.

Trade-offs explícitos:

- **Multi-processo (>1 DEILE escrevendo no mesmo deile.log)**: o rollover
  é executado por UM processo; o outro continua escrevendo no fd antigo
  até reabrir. Aceitável aqui porque cada processo tem seu próprio
  ``FileHandler`` Python (o `[%(process)d]` no formatter desambigua).
  Se dois processos rotacionam simultâneamente, o segundo encontra o
  ``dfn`` já existindo e `_safe_replace` faz `append` por concatenação
  pra não perder dados.
- **macOS/Linux**: `os.replace` é atômico em mesmo filesystem. Tempfile
  → renomeação não é usada porque o source (``deile.log``) já está
  no destino-pai final.
- **Windows**: `os.replace` é atômico e overwrites destino — funciona
  igual a POSIX.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import List, Optional

# Default — pode ser overrideado via construtor / setting.
_DEFAULT_RETENTION_DAYS = 30

# Regex YYYY-MM-DD usada pelo GC (qualquer outra coisa no diretório
# de logs é deixada em paz: arquivos do operador, security_audit.log etc.).
import re as _re  # noqa: E402 — local-only

_DAILY_DIR_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HOUR_FILE_RE = _re.compile(r"^\d{2}\.log$")


class HourlyDailyDirRotatingHandler(TimedRotatingFileHandler):
    """Rotaciona ``baseFilename`` (ex: ``deile.log``) por hora, arquivando
    em sub-diretório diário.

    Parameters
    ----------
    filename
        Caminho do arquivo "current" (ex: ``~/.deile/logs/deile.log``).
        NÃO use um path em subpasta de data aqui — o handler precisa que
        o pai seja o diretório-raiz onde ficam as subpastas diárias.
    retention_days
        Idade máxima (em dias) das subpastas diárias antes do GC. ``0``
        ou negativo desliga o GC (todas as subpastas mantidas — útil
        em testes ou perícia).
    encoding, delay, utc, atTime, errors
        Repassados para :class:`TimedRotatingFileHandler`.
    """

    def __init__(
        self,
        filename: str,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        encoding: Optional[str] = "utf-8",
        delay: bool = False,
        utc: bool = False,
        atTime=None,
        errors: Optional[str] = None,
    ) -> None:
        super().__init__(
            filename=filename,
            when="H",
            interval=1,
            backupCount=0,        # GC próprio (multi-day folders)
            encoding=encoding,
            delay=delay,
            utc=utc,
            atTime=atTime,
            errors=errors,
        )
        self.retention_days = int(retention_days)
        self._logs_root = Path(self.baseFilename).parent
        # `namer` reescreve o filename do rollover; `rotator` faz o
        # mkdir + replace seguro. Anexar AQUI (não inline em emit) é o
        # contrato exposto pelo TimedRotatingFileHandler — ele chama
        # `self.namer(default)` dentro do doRollover.
        self.namer = self._hour_in_daily_dir_namer
        self.rotator = self._safe_rotator

    # ------------------------------------------------------------------
    # rollover plumbing
    # ------------------------------------------------------------------

    def _hour_in_daily_dir_namer(self, default_name: str) -> str:
        """Converte ``<base>.YYYY-MM-DD_HH`` em ``<logs_root>/<YYYY-MM-DD>/<HH>.log``.

        ``default_name`` chega no formato gerado por
        :meth:`TimedRotatingFileHandler.doRollover`:
        ``baseFilename + "." + time.strftime("%Y-%m-%d_%H", t_anterior)``.
        Se o formato divergir (versão de Python exótica), caímos no
        nome default — preserva o log, perde só a organização.
        """
        prefix = self.baseFilename + "."
        if not default_name.startswith(prefix):
            return default_name
        suffix = default_name[len(prefix):]
        # Esperado: "YYYY-MM-DD_HH" — splitamos no underscore.
        parts = suffix.split("_")
        if len(parts) != 2 or len(parts[0]) != 10 or len(parts[1]) != 2:
            return default_name
        day, hour = parts
        try:
            int(hour)  # valida que é numérico
        except ValueError:
            return default_name
        return str(self._logs_root / day / f"{hour}.log")

    def _safe_rotator(self, source: str, dest: str) -> None:
        """Move ``source`` (``deile.log``) para ``dest`` (``YYYY-MM-DD/HH.log``).

        Cria a subpasta diária se não existir e faz GC após o rename.
        Em caso de colisão (mesma hora já arquivada — improvável mas
        possível se relógio do sistema voltou), faz append do source
        no final do dest e remove o source. Nunca perdemos dados.
        """
        try:
            dest_path = Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if dest_path.exists():
                # Colisão rara: append source no final do dest e remove
                # source. Preserva a ordem cronológica.
                with open(source, "rb") as src_fh, open(dest, "ab") as dest_fh:
                    shutil.copyfileobj(src_fh, dest_fh)
                os.unlink(source)
            else:
                os.replace(source, dest)
        except OSError:
            # Falha de FS: deixa source intacto. TimedRotatingFileHandler
            # vai reabrir o stream após este método retornar — pode duplicar
            # algumas linhas, mas não perde nada.
            return
        # GC oportunista após cada rollover (1×/hora) — barato.
        self._purge_old_dirs()

    # ------------------------------------------------------------------
    # GC de subpastas diárias antigas
    # ------------------------------------------------------------------

    def _purge_old_dirs(self) -> None:
        if self.retention_days <= 0:
            return
        try:
            cutoff = datetime.now() - timedelta(days=self.retention_days)
        except OverflowError:
            return
        try:
            entries = list(self._logs_root.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or not _DAILY_DIR_RE.match(entry.name):
                # Não toca em qualquer coisa que não pareça subpasta diária
                # nossa (arquivos do operador, security_audit.log etc.).
                continue
            try:
                dir_date = datetime.strptime(entry.name, "%Y-%m-%d")
            except ValueError:
                continue
            if dir_date < cutoff:
                try:
                    shutil.rmtree(entry)
                except OSError:
                    # Permissão / file in use — deixa pra próxima rodada.
                    continue


# --------------------------------------------------------------------------
# Inventário utilitário — usado pelo painel/CLI para listar arquivos
# disponíveis (current + arquivados). Não é parte do logging Pythonic;
# mora aqui por proximidade do schema.
# --------------------------------------------------------------------------

def list_archived_log_files(logs_root: Path) -> List[Path]:
    """Retorna paths de arquivos rotacionados (ordem cronológica asc).

    Inclui ``<logs_root>/YYYY-MM-DD/HH.log`` mas NÃO inclui o
    ``deile.log`` corrente no raiz. Útil pra ferramentas que precisam
    listar/abrir logs históricos.
    """
    if not logs_root.is_dir():
        return []
    out: List[tuple] = []
    try:
        entries = list(logs_root.iterdir())
    except OSError:
        return []
    for d in entries:
        if not d.is_dir() or not _DAILY_DIR_RE.match(d.name):
            continue
        try:
            files = sorted(d.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for f in files:
            if f.is_file() and _HOUR_FILE_RE.match(f.name):
                out.append((d.name, f.name, f))
    out.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in out]


def current_log_path(logs_root: Path) -> Path:
    """Path do arquivo current (sempre ``<logs_root>/deile.log``).

    Helper apenas pra centralizar o nome. Não toca FS.
    """
    return logs_root / "deile.log"
