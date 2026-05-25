"""Registry — ``~/.deile/run/registry.json`` compartilhado entre processos.

Fase 3 da issue #303. Cada processo DEILE registra sua identidade (instance_id,
PID, role, endpoint, state_file) num arquivo JSON único, com lock de
arquivo POSIX para sincronizar escritas concorrentes (sem fila externa,
sem watchdog — mantém a dependência mínima, alinhada ao espírito de
"procfs-like" da issue).

Operações:
  - :meth:`Registry.register` adiciona/atualiza por ``instance_id`` (idempotente).
  - :meth:`Registry.deregister` remove por ``instance_id``.
  - :meth:`Registry.list` devolve entries; GC opcional remove entries cujo
    PID está morto OU cujo ``state_file`` sumiu.

Concorrência: ``fcntl.flock(LOCK_EX)`` em POSIX. Em Windows o lock vira
no-op (best-effort) — mesma postura do :class:`StatusServer`. Registry
ainda funciona em Windows, só perde a serialização entre processos
(aceitável dado o caso de uso single-host).

Ver decisão #36 (status server + registry) em ``docs/system_design/DECISOES.md``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from deile.runtime.instance_state import (_DEFAULT_RUNTIME_DIR,
                                          _ENV_RUNTIME_DIR, pid_alive)

__all__ = [
    "Registry",
    "RegistryEntry",
    "REGISTRY_SCHEMA_VERSION",
    "DEFAULT_REGISTRY_FILENAME",
]

logger = logging.getLogger(__name__)

REGISTRY_SCHEMA_VERSION = 1
DEFAULT_REGISTRY_FILENAME = "registry.json"


def _is_posix() -> bool:
    return os.name == "posix" and sys.platform != "win32"


def _resolve_runtime_dir(override: Optional[Path]) -> Path:
    """Mesmo loader do :mod:`instance_state` — override > env > default."""
    if override is not None:
        return Path(override)
    env_val = os.environ.get(_ENV_RUNTIME_DIR, "").strip()
    if env_val:
        return Path(env_val)
    return _DEFAULT_RUNTIME_DIR


@dataclass(frozen=True)
class RegistryEntry:
    """Identidade de um processo DEILE registrado.

    Imutável (``frozen=True``) — atualizações criam uma nova instance e
    sobrescrevem por ``instance_id`` no :meth:`Registry.register`.

    Campos:
      - ``instance_id``: identificador estável do processo (ex: ``cli-7c4f1e``).
      - ``pid``: PID atual; usado pelo GC do :meth:`Registry.list`.
      - ``role``: ``cli``/``pipeline``/``bot``/``worker``/``other``.
      - ``started_at``: ISO8601 UTC do startup (cosmético — debug/sort).
      - ``endpoint``: URI do Unix socket (``unix:///abs/path/<id>.sock``);
        string vazia se Status server desabilitado/não-POSIX.
      - ``state_file``: caminho absoluto do state file ``<id>.json``; GC
        remove entries cujo file sumiu (proxy de "processo morreu sem
        cleanup limpo").
    """

    instance_id: str
    pid: int
    role: str
    started_at: str
    endpoint: str
    state_file: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> Optional["RegistryEntry"]:
        """Tolerante: campos ausentes viram default; tipos errados → None."""
        try:
            iid = str(payload.get("instance_id", "")).strip()
            pid = int(payload.get("pid", 0))
        except (TypeError, ValueError):
            return None
        if not iid or pid <= 0:
            return None
        return cls(
            instance_id=iid,
            pid=pid,
            role=str(payload.get("role", "other")),
            started_at=str(payload.get("started_at", "")),
            endpoint=str(payload.get("endpoint", "")),
            state_file=str(payload.get("state_file", "")),
        )


class Registry:
    """Gerencia ``<runtime_dir>/registry.json`` com lock atômico.

    Thread-safe via lock de arquivo (cobre também concorrência inter-processo).
    Operações ``register``/``deregister``/``list`` fazem o ciclo completo
    read-modify-write sob ``LOCK_EX``.

    O GC dentro de :meth:`list` é opcional (``gc=True`` por padrão): remove
    entries cujo PID não está vivo OU cujo ``state_file`` não existe — cobre
    o cenário de ``kill -9``, crash ou shutdown sem :meth:`deregister`.
    """

    def __init__(self, registry_path: Optional[Path] = None) -> None:
        if registry_path is not None:
            self._path = Path(registry_path)
        else:
            runtime_dir = _resolve_runtime_dir(None).resolve()
            self._path = runtime_dir / DEFAULT_REGISTRY_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    # ── operações públicas ────────────────────────────────────────────────

    def register(self, entry: RegistryEntry) -> None:
        """Adiciona ou atualiza ``entry`` (idempotente por ``instance_id``)."""
        if not isinstance(entry, RegistryEntry):
            raise TypeError(f"esperado RegistryEntry, recebido {type(entry)!r}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            entries = self._load_unlocked()
            entries = [e for e in entries if e.instance_id != entry.instance_id]
            entries.append(entry)
            self._dump_unlocked(entries)

    def deregister(self, instance_id: str) -> None:
        """Remove a entry de ``instance_id`` (no-op se ausente)."""
        instance_id = str(instance_id).strip()
        if not instance_id:
            return
        # Evita criar registry.json vazio só para deregister algo que nunca
        # foi registrado.
        if not self._path.exists():
            return
        with self._locked():
            entries = self._load_unlocked()
            filtered = [e for e in entries if e.instance_id != instance_id]
            if len(filtered) == len(entries):
                return  # nada removido
            self._dump_unlocked(filtered)

    def list(self, gc: bool = True) -> List[RegistryEntry]:
        """Lista as entries. Quando ``gc=True``, remove órfãos in-line.

        Órfão = PID morto OU state_file ausente. Ambas as condições são
        proxies para "processo encerrou sem deregister limpo".
        """
        if not self._path.exists():
            return []
        with self._locked():
            entries = self._load_unlocked()
            if not gc:
                return list(entries)
            alive = [e for e in entries if self._is_alive(e)]
            if len(alive) != len(entries):
                # Reescreve se removeu alguma — evita lista voltar a ter
                # órfãos no próximo read.
                self._dump_unlocked(alive)
            return alive

    # ── internos ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_alive(entry: RegistryEntry) -> bool:
        """True quando o processo ainda parece vivo (PID + state_file)."""
        if not pid_alive(entry.pid):
            return False
        # ``state_file`` pode estar vazio (processo registrou sem state file
        # próprio — raro mas possível) — nesse caso só consideramos o PID.
        if not entry.state_file:
            return True
        try:
            return Path(entry.state_file).exists()
        except OSError:
            return True  # incerto = manter (princípio: não GC indevido)

    def _read_payload(self) -> Optional[Dict[str, Any]]:
        """Lê + parseia o registry como dict; None se ausente/inválido/schema errado."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Registry.read(%s) falhou: %s", self._path, exc)
            return None
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Registry %s contém JSON inválido (%s); tratando como vazio.",
                self._path, exc,
            )
            return None
        if not isinstance(payload, dict):
            return None
        try:
            sv = int(payload.get("schema_version", 0))
        except (TypeError, ValueError):
            sv = 0
        if sv != REGISTRY_SCHEMA_VERSION:
            logger.warning(
                "Registry %s schema_version=%s não suportado (esperado %s); ignorando.",
                self._path, sv, REGISTRY_SCHEMA_VERSION,
            )
            return None
        return payload

    def _load_unlocked(self) -> List[RegistryEntry]:
        """Lê e parseia o registry. Caller deve segurar o lock.

        Tolerante: JSON inválido vira lista vazia (loga warning). Schema
        version diferente também — log + lista vazia (forward compat).
        """
        payload = self._read_payload()
        if payload is None:
            return []
        raw_entries = payload.get("instances")
        if not isinstance(raw_entries, list):
            return []
        out: List[RegistryEntry] = []
        seen_ids: set[str] = set()
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            entry = RegistryEntry.from_dict(item)
            if entry is None or entry.instance_id in seen_ids:
                continue
            # Dedupe: mantém o primeiro por instance_id (ordem do file).
            seen_ids.add(entry.instance_id)
            out.append(entry)
        return out

    def _dump_unlocked(self, entries: List[RegistryEntry]) -> None:
        """Escreve o registry atomicamente. Caller deve segurar o lock.

        Padrão write-tmp + ``os.replace`` (mesmo do state file). Usa
        ``ensure_ascii=False`` + ``sort_keys=True`` por consistência com
        :mod:`instance_state`.
        """
        payload = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "instances": [e.to_dict() for e in entries],
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning(
                "Registry.write(%s) falhou: %s", self._path, exc,
            )
            # tenta limpar o tmp se ficou pra trás
            try:
                tmp.unlink()
            except OSError:
                pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Lock exclusivo de arquivo (POSIX). No-op em Windows.

        Usa um lock file separado (``registry.json.lock``) para evitar
        manter FD aberto sobre o próprio registry — assim ``os.replace``
        continua atômico e não esbarra em arquivo "em uso" no Windows.
        """
        if not _is_posix():
            # Windows: sem flock real. Aceitamos o trade-off (single-host,
            # baixa contenção). Documentado.
            yield
            return
        import fcntl  # POSIX-only — import tardio
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        # Garante que o diretório existe (caso primeira chamada antes do
        # registry.json ser criado).
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # ``open(..., 'a+')`` cria se não existe e mantém o descritor.
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                logger.debug("Registry unlock falhou: %s", exc)
            try:
                os.close(fd)
            except OSError:
                pass
