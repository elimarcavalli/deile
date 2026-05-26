"""InstanceState — estado vivo por-processo publicado em ``~/.deile/run/``.

Cada processo DEILE (CLI interativo, deile-pipeline, deile-worker, deilebot,
deile-shell) publica sua verdade autoritativa em um state file local. O painel
universal (``infra/k8s/_panel*``) consulta esses arquivos em vez de inferir
estado por log-tailing (substituído por design — ver issue #303).

Schema (v1)::

    {
      "schema_version": 1,
      "instance_id": "cli-7c4f1e",
      "pid": 28117,
      "role": "cli",
      "started_at": "2026-05-24T22:00:00.000000+00:00",
      "last_heartbeat_at": "2026-05-24T23:15:42.123456+00:00",
      "current_action": {
        "kind": "tool_execution",
        "started_at": "...",
        "detail": "execute_bash",
        "session_id": "sess-9f2",
        "model": "deepseek:v4-pro"
      } | null,
      "stats": {
        "tokens_in": int, "tokens_out": int, "cost_usd": float,
        "turns": int, "tool_calls": int, "errors": int
      }
    }

Regras invioláveis (ver pilar 08-SEGURANCA e pilar 06-MEMORIA):
  - Nenhum segredo, prompt, tool_args ou conteúdo de mensagem entra no state file.
  - ``current_action.detail`` é um *short label* (max 80 chars).

Atomicidade: o flush escreve em ``<id>.json.tmp`` e faz ``os.replace`` —
atômico em POSIX. Em Windows é "atômico o suficiente" se o destino existir
(documentado, não otimizado).

Async: ``heartbeat_loop`` é uma task asyncio; o flush é síncrono (``write_text``
+ ``os.replace`` é <1ms em SSD) e roda direto no event loop sem ``to_thread``
para minimizar latência e simplificar o ciclo de vida da task. Princípio 1
(Async-First) é respeitado para qualquer I/O que não seja triviamente rápido.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from deile.runtime.registry import Registry, RegistryEntry
    from deile.runtime.status_server import StatusServer

__all__ = [
    "InstanceState",
    "get_instance_state",
    "peek_instance_state",
    "reset_instance_state",
    "pid_alive",
    "VALID_ROLES",
    "VALID_ACTION_KINDS",
    "DETAIL_MAX_LEN",
    "SCHEMA_VERSION",
]

# Plain stdlib logger — não usar ``deile.storage.logs.get_logger`` no escopo
# de módulo: aquele setter força ``logger.propagate=False`` na inicialização,
# o que quebra ``caplog`` da pytest em testes de outros módulos quando este
# arquivo é importado primeiro. Mensagens deste logger ainda fluem para o
# logger ``deile`` por propagação normal.
logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

VALID_ROLES = frozenset({"cli", "pipeline", "bot", "worker", "other"})
VALID_ACTION_KINDS = frozenset(
    {"idle", "starting", "tool_execution", "llm_call", "shutting_down"}
)
DETAIL_MAX_LEN = 80

_ENV_RUNTIME_DIR = "DEILE_RUNTIME_DIR"
_DEFAULT_RUNTIME_DIR = Path.home() / ".deile" / "run"


def _utc_now_iso() -> str:
    """ISO8601 timestamp em UTC com sufixo ``+00:00`` (formato do schema)."""
    return datetime.now(timezone.utc).isoformat()


def _resolve_runtime_dir(override: Optional[Path]) -> Path:
    """Resolve o runtime dir respeitando override > env > default."""
    if override is not None:
        return Path(override)
    env_val = os.environ.get(_ENV_RUNTIME_DIR, "").strip()
    if env_val:
        return Path(env_val)
    return _DEFAULT_RUNTIME_DIR


def _supports_status_server() -> bool:
    """Status server (Unix socket) só roda em POSIX. Windows vira no-op
    silencioso no :class:`InstanceState`, sem warning até :meth:`start`."""
    return os.name == "posix" and sys.platform != "win32"


def pid_alive(pid: int) -> bool:
    """Retorna True se ``pid`` está vivo (ou inacessível por permissão).

    Usa ``os.kill(pid, 0)`` — o signal 0 não envia nada, só valida que o
    processo existe e que temos algum acesso. Distingue três casos:

      - ``ProcessLookupError`` (ESRCH) → processo não existe → False.
      - ``PermissionError`` (EPERM)    → processo existe mas pertence a outro
        usuário (ainda é "vivo" para o painel — só não podemos sinalizar) →
        True.
      - sucesso silencioso → True.

    Em Windows, ``os.kill`` se comporta diferente; este helper é best-effort —
    em caso de qualquer outro ``OSError`` retornamos True para evitar GC
    indevido (princípio: false positive de vivo é melhor que false negative).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        logger.debug("pid_alive(%s) inconclusive OSError: %s", pid, exc)
        return True
    return True


class InstanceState:
    """Estado vivo publicado pelo processo em ``~/.deile/run/<id>.json``.

    Thread-safe: ``update_action`` / ``update_stats`` / ``clear_action`` /
    ``heartbeat_loop`` podem ser chamados de qualquer thread/coroutine. Um
    ``threading.Lock`` protege a mutação do dict e o flush atômico.

    Ciclo de vida:
      - ``__init__`` cria o state file imediatamente e registra ``atexit``.
      - ``heartbeat_loop`` deve ser agendado como task asyncio pelo bootstrap.
      - ``close()`` remove o state file (idempotente; chamado por atexit OU
        manualmente). Após ``close()``, todos os updates viram no-op silencioso.

    Cross-platform: ``Path.replace`` é atômico em POSIX e "atômico o
    suficiente" em Windows quando o destino já existe — em rotações raras
    pode haver janela de race. Suporte oficial é POSIX (macOS + Linux).
    """

    def __init__(
        self,
        role: str,
        runtime_dir: Optional[Path] = None,
        *,
        enable_status_server: bool = True,
        enable_registry: bool = True,
    ) -> None:
        if role not in VALID_ROLES:
            raise ValueError(
                f"role inválido: {role!r}. Esperado um de {sorted(VALID_ROLES)}"
            )

        self._role = role
        self._runtime_dir = _resolve_runtime_dir(runtime_dir).resolve()
        self._runtime_dir.mkdir(parents=True, exist_ok=True)

        self._instance_id = f"{role}-{uuid.uuid4().hex[:8]}"
        self._path = self._runtime_dir / f"{self._instance_id}.json"
        self._tmp_path = self._path.with_suffix(".tmp")

        self._lock = threading.Lock()
        self._closed = False

        now = _utc_now_iso()
        self._state: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "instance_id": self._instance_id,
            "pid": os.getpid(),
            "role": role,
            "started_at": now,
            "last_heartbeat_at": now,
            "current_action": None,
            "stats": {
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
                "turns": 0,
                "tool_calls": 0,
                "errors": 0,
            },
        }
        self._flush_unlocked()

        # Fase 2/3 (issue #303): status server + registry. Instanciados aqui
        # mas o socket só é aberto em ``start_async_tasks`` (precisa do event
        # loop). O registry é síncrono e roda imediatamente — o painel
        # consegue ver o processo antes mesmo do bootstrap async terminar.
        self._status_server: Optional["StatusServer"] = None
        self._registry: Optional["Registry"] = None
        if enable_status_server and _supports_status_server():
            try:
                from deile.runtime.status_server import StatusServer
                self._status_server = StatusServer(self)
            except Exception as exc:  # noqa: BLE001 — best-effort observability
                logger.warning(
                    "Falha ao instanciar StatusServer (id=%s): %s",
                    self._instance_id, exc,
                )
                self._status_server = None
        if enable_registry:
            try:
                from deile.runtime.registry import Registry
                self._registry = Registry(
                    registry_path=self._runtime_dir / "registry.json"
                )
                self._registry.register(self._build_registry_entry())
            except Exception as exc:  # noqa: BLE001 — best-effort observability
                logger.warning(
                    "Falha ao registrar no Registry (id=%s): %s",
                    self._instance_id, exc,
                )
                self._registry = None

        atexit.register(self.close)
        logger.debug(
            "InstanceState created: id=%s path=%s pid=%s status_server=%s registry=%s",
            self._instance_id, self._path, os.getpid(),
            self._status_server is not None, self._registry is not None,
        )

    # ── identidade ────────────────────────────────────────────────────────

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def role(self) -> str:
        return self._role

    @property
    def runtime_dir(self) -> Path:
        return self._runtime_dir

    @property
    def status_server(self) -> Optional["StatusServer"]:
        """Acesso ao status server (None se desabilitado/Windows). Útil para
        o bootstrap agendar :meth:`StatusServer.serve_forever` como task."""
        return self._status_server

    @property
    def registry(self) -> Optional["Registry"]:
        """Acesso ao registry (None se desabilitado). Útil para queries
        ad-hoc no painel/diagnóstico."""
        return self._registry

    def _build_registry_entry(self) -> "RegistryEntry":
        """Constrói a entry do registry com a identidade atual."""
        from deile.runtime.registry import RegistryEntry
        endpoint = ""
        if self._status_server is not None:
            try:
                endpoint = self._status_server.endpoint
            except Exception:  # noqa: BLE001
                endpoint = ""
        return RegistryEntry(
            instance_id=self._instance_id,
            pid=os.getpid(),
            role=self._role,
            started_at=str(self._state.get("started_at", "")),
            endpoint=endpoint,
            state_file=str(self._path),
        )

    # ── async lifecycle ───────────────────────────────────────────────────

    async def start_async_tasks(
        self, *, heartbeat_interval_s: float = 2.0,
    ) -> List[asyncio.Task]:
        """Inicia heartbeat + status server (se habilitado).

        Devolve a lista de tasks asyncio criadas; o caller (bootstrap)
        retém para cancelar limpo no shutdown. Padrão pensado para o
        :class:`_DeileCLI`, mas qualquer processo (worker, bot, pipeline)
        pode usar — único requisito é estar dentro de um event loop.

        Idempotente: se as tasks já existem (caller chamou duas vezes),
        cria as faltantes e devolve a união.
        """
        tasks: List[asyncio.Task] = []
        tasks.append(asyncio.create_task(
            self.heartbeat_loop(interval_s=heartbeat_interval_s),
            name=f"hb-{self._instance_id}",
        ))
        if self._status_server is not None:
            try:
                await self._status_server.start()
                tasks.append(asyncio.create_task(
                    self._status_server.serve_forever(),
                    name=f"ss-{self._instance_id}",
                ))
            except Exception as exc:  # noqa: BLE001 — observability best-effort
                logger.warning(
                    "StatusServer falhou ao iniciar (id=%s): %s",
                    self._instance_id, exc,
                )
        return tasks

    # ── heartbeat ─────────────────────────────────────────────────────────

    async def heartbeat_loop(self, interval_s: float = 2.0) -> None:
        """Task asyncio que atualiza ``last_heartbeat_at`` a cada ``interval_s``.

        O ``CancelledError`` é re-raised (princípio 6 — error handling).
        Qualquer outra exceção é logada e a task continua o próximo ciclo
        (heartbeat é best-effort: uma falha pontual de I/O não deve parar
        o processo nem matar a task).
        """
        if interval_s <= 0:
            raise ValueError(f"interval_s deve ser > 0, recebido {interval_s}")
        try:
            while not self._closed:
                await asyncio.sleep(interval_s)
                if self._closed:
                    return
                try:
                    self._heartbeat()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — heartbeat é best-effort
                    logger.warning(
                        "InstanceState heartbeat flush failed (id=%s): %s",
                        self._instance_id, exc,
                    )
        except asyncio.CancelledError:
            logger.debug("heartbeat_loop cancelled for %s", self._instance_id)
            raise

    def _heartbeat(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._state["last_heartbeat_at"] = _utc_now_iso()
            self._flush_unlocked()

    # ── ações / stats ─────────────────────────────────────────────────────

    def update_action(
        self,
        kind: str,
        detail: str = "",
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Substitui ``current_action`` e faz flush atômico.

        Validação:
          - ``kind`` deve estar em ``VALID_ACTION_KINDS``.
          - ``detail`` é truncado em ``DETAIL_MAX_LEN`` (80) chars.

        Não armazena tool_args, prompts ou qualquer conteúdo livre.
        ``session_id`` e ``model`` são identificadores opacos (UUID/handle),
        nunca conteúdo (regra do pilar 08).
        """
        if kind not in VALID_ACTION_KINDS:
            raise ValueError(
                f"kind inválido: {kind!r}. Esperado um de {sorted(VALID_ACTION_KINDS)}"
            )
        clean_detail = (detail or "")[:DETAIL_MAX_LEN]
        action: Dict[str, Any] = {
            "kind": kind,
            "started_at": _utc_now_iso(),
            "detail": clean_detail,
        }
        if session_id is not None:
            action["session_id"] = str(session_id)
        if model is not None:
            action["model"] = str(model)

        with self._lock:
            if self._closed:
                return
            self._state["current_action"] = action
            self._state["last_heartbeat_at"] = _utc_now_iso()
            self._flush_unlocked()

    def clear_action(self) -> None:
        """Define ``current_action = None`` e faz flush."""
        with self._lock:
            if self._closed:
                return
            self._state["current_action"] = None
            self._state["last_heartbeat_at"] = _utc_now_iso()
            self._flush_unlocked()

    def update_stats(
        self,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        turns: int = 0,
        tool_calls: int = 0,
        errors: int = 0,
    ) -> None:
        """Acumula valores nos contadores existentes (NÃO substitui).

        Todos os parâmetros são keyword-only e default zero — chamadas
        parciais ficam concisas: ``update_stats(tool_calls=1)``.
        """
        with self._lock:
            if self._closed:
                return
            stats = self._state["stats"]
            stats["tokens_in"] = int(stats["tokens_in"]) + int(tokens_in)
            stats["tokens_out"] = int(stats["tokens_out"]) + int(tokens_out)
            stats["cost_usd"] = float(stats["cost_usd"]) + float(cost_usd)
            stats["turns"] = int(stats["turns"]) + int(turns)
            stats["tool_calls"] = int(stats["tool_calls"]) + int(tool_calls)
            stats["errors"] = int(stats["errors"]) + int(errors)
            self._state["last_heartbeat_at"] = _utc_now_iso()
            self._flush_unlocked()

    # ── leitura ───────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Cópia profunda do estado atual (segura para inspeção externa)."""
        with self._lock:
            return deepcopy(self._state)

    # ── shutdown ──────────────────────────────────────────────────────────

    def close(self) -> None:
        """Idempotente. Remove o state file. Chamado por atexit ou manualmente.

        Após ``close()``, mutadores viram no-op silencioso (não levantam),
        para tolerar chamadas tardias durante o teardown do interpretador.

        Fase 2/3 (issue #303): também:
          - deregistra do :class:`Registry` se ativo (síncrono);
          - tenta parar o :class:`StatusServer` se ativo. Como ``close()`` é
            síncrono (atexit), o stop assíncrono é feito best-effort:
            agenda no event loop ativo se houver; caso contrário só apaga
            o socket file (o OS limpa o resto).
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for candidate in (self._path, self._tmp_path):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.warning(
                        "InstanceState.close: could not remove %s: %s",
                        candidate, exc,
                    )

        # Fora do lock: chamadas externas (Registry/StatusServer) podem
        # tentar reentrar no state — e estamos no caminho de shutdown,
        # ninguém deveria mais mutar.
        if self._registry is not None:
            try:
                self._registry.deregister(self._instance_id)
            except Exception as exc:  # noqa: BLE001 — shutdown não levanta
                logger.debug(
                    "Registry.deregister(%s) falhou: %s",
                    self._instance_id, exc,
                )
        if self._status_server is not None:
            self._shutdown_status_server_best_effort()

    def _shutdown_status_server_best_effort(self) -> None:
        """Para o status server respeitando o estado do event loop.

        Casos:
          1. Loop rodando → agenda ``stop()`` como task (fire-and-forget).
             O caller do shutdown limpo (``_DeileCLI._shutdown_instance_state``)
             prefere :meth:`StatusServer.stop` direto via ``await``.
          2. Sem loop ativo (atexit) → apaga só o socket file. O servidor
             em si será destruído com o processo; o file leftover sumiria
             pelo restart limpo, mas removemos por boa cidadania.
        """
        server = self._status_server
        if server is None:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            try:
                loop.create_task(server.stop())
                return
            except RuntimeError:
                pass
        # Sem loop: melhor remover o socket file pra evitar leftover
        # confundir o painel/outros processos.
        try:
            socket_path = server.socket_path
        except AttributeError:
            return
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug(
                "InstanceState.close: socket unlink %s falhou: %s",
                socket_path, exc,
            )

    # ── internals ─────────────────────────────────────────────────────────

    def _flush_unlocked(self) -> None:
        """Escreve o estado atomicamente. Caller deve segurar ``self._lock``.

        Usa ``write_text`` em ``<id>.json.tmp`` seguido de ``os.replace`` —
        atômico em POSIX, "atômico o suficiente" em Windows. Falha de I/O
        é logada (warning) mas não levantada: o painel se vira com staleness
        bounded.
        """
        try:
            payload = json.dumps(self._state, ensure_ascii=False, sort_keys=True)
            self._tmp_path.write_text(payload, encoding="utf-8")
            os.replace(self._tmp_path, self._path)
        except OSError as exc:
            logger.warning(
                "InstanceState flush failed (id=%s, path=%s): %s",
                self._instance_id, self._path, exc,
            )


# ── singleton ─────────────────────────────────────────────────────────────

_instance_singleton: Optional[InstanceState] = None
_singleton_lock = threading.Lock()


def get_instance_state(
    role: str = "other",
    runtime_dir: Optional[Path] = None,
) -> InstanceState:
    """Retorna o ``InstanceState`` singleton do processo (cria no primeiro acesso).

    Quando já existe, ``role`` e ``runtime_dir`` são ignorados — o primeiro
    caller define a identidade do processo. O agente CLI deve chamar este
    factory cedo no bootstrap (em ``_DeileCLI.initialize``) com ``role="cli"``.

    Para testes: use :func:`reset_instance_state` entre invocações para
    forçar uma nova instância.
    """
    global _instance_singleton
    with _singleton_lock:
        if _instance_singleton is None:
            _instance_singleton = InstanceState(role=role, runtime_dir=runtime_dir)
        return _instance_singleton


def reset_instance_state() -> None:
    """Fecha o singleton atual e zera a referência. Apenas para testes.

    Em produção o singleton vive até o teardown do processo (``atexit``).
    """
    global _instance_singleton
    with _singleton_lock:
        if _instance_singleton is not None:
            _instance_singleton.close()
            _instance_singleton = None


def peek_instance_state() -> Optional[InstanceState]:
    """Retorna o singleton atual SEM criar um se ainda não existe.

    Diferente de :func:`get_instance_state`, esta acessor não tem side-effect:
    nada é instanciado, nenhum state file é publicado, nenhuma task asyncio é
    agendada. Pensada para *sub-readers* (surfaces de UI, painel, observador
    externo) que querem ler o estado vivo apenas se algum bootstrap (CLI,
    worker, bot, pipeline) já tomou ownership do processo. Caso contrário,
    devolve ``None`` e o caller decide degradar para placeholders.

    Issue #317 + DECISOES #37/#38: o singleton é dono do state file + atexit;
    sub-readers consomem, nunca produzem.
    """
    return _instance_singleton
