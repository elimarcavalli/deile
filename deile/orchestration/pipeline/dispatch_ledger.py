"""dispatch_ledger — mini-ledger persistido em arquivo JSON único no PVC
do pipeline pra rastrear ``{pr|issue: <N> → task_id, session_id, ...}``.

Issue #309 fase 3.5 (resume mecânica): o pipeline grava aqui o ``task_id``
+ ``session_id`` retornados pelo worker no dispatch original, e consulta
no próximo tick pra montar payload de resume (``resume_session_id +
prev_task_id``) em vez de re-dispatch fresh.

Design:
- **Single file, single writer.** O pipeline roda em 1 réplica
  (``strategy: Recreate``), então atomic write-tmp+replace basta — sem
  precisar de SQLite, etcd, ou fcntl.flock. Hot path do tick fica O(1).
- **Best-effort.** Falhas de I/O viram ``logger.warning`` e operação
  no-op. O pipeline ainda funciona sem resume (cai em fresh dispatch).
- **Schema versionado.** Campo ``version`` no JSON root permite migrações
  futuras sem quebrar leituras de versões antigas.
- **Chave canonical.** ``pr:<N>`` ou ``issue:<N>`` — estáveis entre ticks,
  worker-agnostic (mesma chave pra deile-worker e claude-worker).

Não substitui o `resume_block` da issue #254 (que viaja no payload do
dispatch e carrega progresso do worker DEILE em-flight) — é
complementar e foca em PERSISTIR a identidade do trabalho (task_id +
session_id do claude) entre dispatches separados.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Schema version. Bump ao mudar estrutura — leitura faz best-effort de
#: migração in-place; escrita sempre usa a versão corrente.
LEDGER_SCHEMA_VERSION = 1


def _default_ledger_path() -> Path:
    """Resolve o path do ledger. Env var ``DEILE_PIPELINE_LEDGER_PATH``
    sobrescreve (útil em testes); default = ``~/.deile/pipeline/dispatches.json``.
    """
    env = os.environ.get("DEILE_PIPELINE_LEDGER_PATH", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".deile" / "pipeline" / "dispatches.json"


class DispatchLedger:
    """Mini-ledger JSON persistido — ``{key → dispatch_record}``.

    Thread-safety: NÃO é thread-safe. O pipeline roda 1 réplica em single
    thread asyncio; concorrência intra-process não acontece. Multi-process
    requereria flock + retry — out of scope (issue separada).

    Atomicidade de WRITE: ``write-tmp + os.replace`` garante que leitor
    nunca vê estado parcial. Atomicidade de READ-MODIFY-WRITE: NÃO é
    atomic — uma escrita pode sobrescrever modificação concorrente. OK
    no design single-writer.

    Crash-safety: o ledger pode estar STALE (worker terminou mas
    pipeline morreu antes do `clear`). Stale records são detectados pelo
    consumidor (worker retorna 404/410 no resume-info → fallback fresh
    + clear stale).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _default_ledger_path()
        self._cache: Optional[Dict[str, Any]] = None
        #: ``(st_mtime_ns, st_size)`` of the file when cache was last loaded.
        #: ``None`` means cache is invalid (not yet loaded, or invalidated).
        self._file_sig: Optional[tuple] = None

    @staticmethod
    def key_for_pr(number: int) -> str:
        return f"pr:{number}"

    @staticmethod
    def key_for_issue(number: int) -> str:
        return f"issue:{number}"

    # ----------------------------------------------------------------- #
    # Persistence primitives
    # ----------------------------------------------------------------- #

    def _load(self) -> Dict[str, Any]:
        """Lê o ledger do disco. None → empty (file ausente/malformed).

        Stat-based cache invalidation (issue #507 #11b): when a cached copy
        exists, we do a cheap ``os.stat`` to check whether the file's
        ``(st_mtime_ns, st_size)`` changed before trusting the in-memory
        copy.  This lets a second long-lived process (e.g. a separate reader
        instance) detect writes made by the single writer without having to
        call ``invalidate_cache()`` explicitly.

        Signature uses **both** mtime_ns **and** size so that two successive
        writes that happen within the same nanosecond (possible on FAT /
        second-granularity mtime filesystems) but produce different sizes are
        still detected correctly.

        When the cache is empty (first call or after ``invalidate_cache()``)
        we skip the stat and fall straight through to the read path — there
        is nothing to validate.
        """
        if self._cache is not None:
            # --- stat-based cache validation ---
            if not self._path.exists():
                # File disappeared: treat as empty cache.
                self._cache = {"version": LEDGER_SCHEMA_VERSION, "dispatches": {}}
                self._file_sig = None
                return self._cache
            try:
                st = os.stat(self._path)
                current_sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                # Cannot stat; preserve the cached copy (best-effort).
                return self._cache
            if current_sig == self._file_sig:
                # Signature unchanged — trust cached copy.
                return self._cache
            # Signature changed — invalidate and fall through to reload.
            self._cache = None
            self._file_sig = None

        if not self._path.exists():
            self._cache = {"version": LEDGER_SCHEMA_VERSION, "dispatches": {}}
            return self._cache
        try:
            raw = self._path.read_text(encoding="utf-8")
            # Record signature AFTER a successful read so we capture the
            # on-disk state that corresponds to the content we just loaded.
            try:
                st = os.stat(self._path)
                self._file_sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._file_sig = None
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("ledger %s corrupted/unreadable, starting empty: %s",
                           self._path, exc)
            self._cache = {"version": LEDGER_SCHEMA_VERSION, "dispatches": {}}
            return self._cache
        if not isinstance(data, dict) or "dispatches" not in data:
            self._cache = {"version": LEDGER_SCHEMA_VERSION, "dispatches": {}}
            return self._cache
        # Best-effort migration: bump version field se for menor (no-op p/ V1).
        data.setdefault("version", LEDGER_SCHEMA_VERSION)
        data.setdefault("dispatches", {})
        self._cache = data
        return data

    def _flush(self) -> None:
        """Atomic write do ledger pro disco. Falha de I/O → warning."""
        if self._cache is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._cache, indent=2, sort_keys=True))
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("failed to flush ledger %s: %s", self._path, exc)

    # ----------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------- #

    def record(
        self,
        key: str,
        *,
        task_id: str,
        session_id: str,
        stage: Optional[str] = None,
        branch: Optional[str] = None,
        worker_kind: Optional[str] = None,
    ) -> None:
        """Grava (cria ou atualiza) dispatch record. Atualiza ``last_seen_at``
        e mantém ``first_seen_at`` do registro original (pra reaper calcular
        idade total)."""
        if not key or not task_id:
            logger.warning("ledger.record: skipping empty key=%r or task_id=%r",
                           key, task_id)
            return
        data = self._load()
        now = int(time.time())
        existing = data["dispatches"].get(key, {})
        data["dispatches"][key] = {
            "task_id": task_id,
            "session_id": session_id or "",
            "stage": stage,
            "branch": branch,
            "worker_kind": worker_kind,
            "first_seen_at": existing.get("first_seen_at", now),
            "last_seen_at": now,
            "attempt": int(existing.get("attempt", 0)) + 1,
        }
        self._flush()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Retorna o record ou None. Cópia rasa (modificação não persiste
        — use ``record`` pra atualizar)."""
        if not key:
            return None
        data = self._load()
        record = data["dispatches"].get(key)
        return dict(record) if record else None

    def clear(self, key: str) -> None:
        """Remove o record (call após work bem-sucedido). No-op se ausente."""
        if not key:
            return
        data = self._load()
        if key in data["dispatches"]:
            data["dispatches"].pop(key)
            self._flush()

    def list_all(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot raso de todos os records (debugging / reaper / painel)."""
        data = self._load()
        return {k: dict(v) for k, v in data["dispatches"].items()}

    def invalidate_cache(self) -> None:
        """Força reload do disco no próximo acesso. Útil em testes."""
        self._cache = None
        self._file_sig = None
