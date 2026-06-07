#!/usr/bin/env python3
"""_worker_core — núcleo agnóstico-de-CLI compartilhado pelos workers headless.

Extraído do ``claude_worker_server.py`` (issue multi-CLI worker fleet, Fase A1)
para que o servidor genérico ``cli_worker_server.py`` e o ``claude_worker_server``
compartilhem a mesma maquinaria sem duplicação. Tudo aqui é **independente do CLI
concreto** que roda no pod (claude, opencode, aider, ...):

* Lease por workspace (``.lease.json``): aquisição atômica multi-réplica,
  heartbeat, release e marcação do PID do subprocess.
* Liveness genérica: ``pid_alive`` (POSIX ``kill -0``) e ``lease_is_stale``
  (TTL + PID + presença de pod).
* Execução de subprocess one-shot com persistência de stdout/stderr para o PVC
  (consumido pelo ``/v1/progress/{task_id}``) e timeout que mata o processo.
* Helpers HTTP: factory de middleware Bearer (whitelist parametrizável) e
  rate-limiter por ator (sliding-window in-memory).
* Utilidades de filesystem: ``dir_bytes`` e validação de ``task_id``.

Os dois servidores mantêm seus nomes públicos (``_acquire_lease``,
``_heartbeat_loop``, etc.) — quando precisam honrar uma constante de módulo
monkeypatchável nos testes (``_LEASE_TTL_S``/``_LEASE_HEARTBEAT_S``), o servidor
expõe um wrapper fino que repassa essa constante a estas funções (que recebem o
valor por parâmetro). O específico-do-CLI (argv, parsing de saída, auth/OAuth)
fica no servidor concreto.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from aiohttp import web

logger = logging.getLogger("deile.worker_core")

#: ``secrets.token_hex(8)`` gera exatamente 16 chars hex; qualquer outra forma é
#: rejeitada para não permitir path traversal pela URL nem leitura de arquivos
#: arbitrários no PVC.
TASK_ID_RE = re.compile(r"[0-9a-f]{16}")

#: Defaults de lease (TTL e heartbeat). Os servidores concretos sobrescrevem
#: via suas próprias constantes monkeypatcháveis, passando o valor por parâmetro.
DEFAULT_LEASE_TTL_S: int = 30
DEFAULT_LEASE_HEARTBEAT_S: int = 5


# --------------------------------------------------------------------------- #
# Utilidades genéricas
# --------------------------------------------------------------------------- #


def validate_task_id_for_path(task_id: str) -> bool:
    """Defesa contra path traversal: task_id deve ser hex 16-char."""
    return bool(TASK_ID_RE.fullmatch(task_id)) if task_id else False


def pid_alive(pid: int) -> bool:
    """True se o PID ainda está vivo neste sistema (POSIX)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def dir_bytes(path: Path) -> int:
    """Soma recursiva dos tamanhos dos arquivos em *path*."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


# --------------------------------------------------------------------------- #
# Lease por workspace (multi-réplica, atomic)
# --------------------------------------------------------------------------- #


async def acquire_lease(
    workspace: Path,
    *,
    ttl_s: int = DEFAULT_LEASE_TTL_S,
    channel: str = "",
    session_id: str = "",
) -> Optional[dict]:
    """Tenta adquirir o lease do workspace.

    Algoritmo:
    1. Se ``.lease.json`` existir e ``heartbeat_at`` for recente (< ``ttl_s``),
       outro pod está ativo → retorna None.
    2. JSON inválido / ausente / heartbeat expirado → considera morto,
       adquire sobrescrevendo.
    3. Write atomic: escreve em ``.lease.tmp.<pod>`` e faz rename pra
       ``.lease.json``. POSIX garante que o rename é atômico.
    4. Re-lê o arquivo para confirmar que nosso pod ganhou a corrida
       (se outro pod fez rename simultaneamente, o arquivo conterá o pod
       dele, e retornamos None).

    Returns:
        dict com o conteúdo do lease quando adquirido; None quando falhou.
    """
    lease_path = workspace / ".lease.json"
    pod_id = os.environ.get("HOSTNAME", f"local-{os.getpid()}")
    now = time.time()

    # Passo 1: verifica lease existente (I/O bloqueante → thread).
    def _check_existing() -> bool:
        """True se lease existente está dentro do TTL (ativo por outro pod)."""
        if not lease_path.exists():
            return False
        try:
            current = json.loads(lease_path.read_text(encoding="utf-8"))
            heartbeat_age = now - float(current.get("heartbeat_at", 0))
            return heartbeat_age < ttl_s
        except (OSError, json.JSONDecodeError, ValueError):
            return False  # corrupto → trata como morto

    if await asyncio.to_thread(_check_existing):
        return None  # workspace em uso por outro pod ativo

    # Passo 2: escreve candidato atômico.
    lease = {
        "pod": pod_id,
        "pid": os.getpid(),
        "started_at": now,
        "heartbeat_at": now,
    }
    # Channel + session_id habilitam o dedup cross-workdir de fresh dispatches:
    # uma 2ª dispatch fresca para o mesmo channel é recusada enquanto o 1º
    # subprocess ainda está vivo. Gravados no acquire (antes do spawn) para
    # existirem cedo o bastante para o scan.
    if channel:
        lease["channel"] = channel
    if session_id:
        lease["session_id"] = session_id

    def _write_and_confirm() -> Optional[dict]:
        tmp = workspace / f".lease.tmp.{pod_id}"
        try:
            tmp.write_text(json.dumps(lease), encoding="utf-8")
            tmp.rename(lease_path)
            # Passo 3: re-lê para confirmar vitória na corrida.
            confirmed = json.loads(lease_path.read_text(encoding="utf-8"))
            if confirmed.get("pod") != pod_id:
                # Outro pod ganhou a corrida atomicamente — não somos o dono.
                return None
            return confirmed
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("lease write/confirm falhou para %s: %s", workspace, exc)
            # Cleanup do tmp se sobrou (best-effort).
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    return await asyncio.to_thread(_write_and_confirm)


async def update_lease_subprocess_pid(
    lease_path: Path, subprocess_pid: Optional[int]
) -> None:
    """Set/clear ``claude_pid`` on the lease — o PID do subprocess do CLI spawnado
    (NÃO o processo do servidor wrapper, que vive em ``pid``).

    Without this field, ``mtime`` of ``.lease.json`` is whatever the heartbeat
    task last wrote (always recent while the server is up) and the ``pid`` is
    just the wrapper — an observer cannot tell whether a real workload is still
    running. With it, ``claude_pid is not None and pid_alive(claude_pid)`` is the
    ground truth for "the CLI is actively working on this workdir". Best-effort:
    a missing/unwritable lease is logged and ignored — the dispatch is the source
    of truth, the lease is just an observability aid.

    O campo permanece nomeado ``claude_pid`` por compatibilidade com observadores
    existentes (painel, ``/v1/pod-status``); semanticamente é o PID do subprocess
    do CLI do worker, seja ele claude ou outro.
    """
    def _update() -> None:
        try:
            content = json.loads(lease_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if subprocess_pid is None:
            content.pop("claude_pid", None)
        else:
            content["claude_pid"] = int(subprocess_pid)
        try:
            tmp = lease_path.with_suffix(".json.pid_tmp")
            tmp.write_text(json.dumps(content), encoding="utf-8")
            tmp.rename(lease_path)
        except OSError as exc:
            logger.warning("lease claude_pid update failed for %s: %s", lease_path, exc)

    await asyncio.to_thread(_update)


async def release_lease(lease_path: Path) -> None:
    """Remove o arquivo de lease. Idempotente: FileNotFoundError é ignorado."""
    def _unlink() -> None:
        try:
            lease_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("falha ao remover lease %s: %s", lease_path, exc)

    await asyncio.to_thread(_unlink)


async def heartbeat_loop(
    lease_path: Path,
    stop_event: asyncio.Event,
    *,
    heartbeat_s: int = DEFAULT_LEASE_HEARTBEAT_S,
) -> None:
    """Atualiza ``heartbeat_at`` no lease a cada ``heartbeat_s`` segundos.

    Best-effort: erros de I/O são logados e ignorados — o heartbeat pode deixar
    de ser atualizado sem derrubar o dispatch. Se o pod perder acesso ao PVC
    (improvável em k3s single-node), o pipeline detectará o TTL expirado e
    tentará adquirir o lease no próximo tick.

    A task é cancelada (ou ``stop_event`` é setado) quando o dispatch terminar.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(heartbeat_s))
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break

        def _update() -> None:
            try:
                content = json.loads(lease_path.read_text(encoding="utf-8"))
                content["heartbeat_at"] = time.time()
                # Write atômico para não corromper o lease se o processo for
                # interrompido no meio da escrita.
                tmp = lease_path.with_suffix(".json.hb_tmp")
                tmp.write_text(json.dumps(content), encoding="utf-8")
                tmp.rename(lease_path)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("heartbeat update falhou em %s: %s", lease_path, exc)

        await asyncio.to_thread(_update)


def lease_is_stale(
    lease_path: Path,
    *,
    ttl_s: int = DEFAULT_LEASE_TTL_S,
    alive_pods: Optional[set] = None,
) -> bool:
    """True se o lease expirou E o PID proprietário não está mais vivo.

    Quando ``alive_pods`` é fornecido (registro de presença), também retorna True
    imediatamente se o pod dono não constar no conjunto de vivos — fechando a
    janela de recuperação de 30 min para ~60 s.

    Conservador: se não conseguir ler o lease, assume que NÃO é stale (fail-safe
    — evita apagar workdirs em uso quando o FS está lento).
    """
    try:
        data = json.loads(lease_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    # Verificação proativa via presença: se o conjunto está disponível e o pod
    # dono não aparece nele, o lease é stale independente do heartbeat.
    if alive_pods is not None:
        pod = data.get("pod", "")
        if pod and pod not in alive_pods:
            return True
    heartbeat_at = data.get("heartbeat_at", 0)
    if (time.time() - float(heartbeat_at)) < ttl_s:
        return False  # heartbeat recente → ativo
    pid = data.get("pid")
    if pid and pid_alive(int(pid)):
        return False  # TTL expirado mas PID ainda vivo → conservador
    return True


# --------------------------------------------------------------------------- #
# Execução de subprocess one-shot com persistência de progresso
# --------------------------------------------------------------------------- #


@dataclass
class SubprocessResult:
    """Resultado de :func:`run_subprocess_with_progress`.

    Encapsula o que o handler precisa devolver na resposta JSON. ``stdout`` e
    ``stderr`` aqui são as strings completas (não truncadas); a truncagem por
    bytes vive no handler, próxima do contrato de resposta.
    """

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


async def run_subprocess_with_progress(
    args: list,
    *,
    cwd: Path,
    task_id: str,
    timeout: int,
    lease_path: Optional[Path] = None,
    root: Optional[Path] = None,
) -> SubprocessResult:
    """Spawn do subprocess do CLI com persistência de stdout/stderr para o PVC.

    Os arquivos ``<task_id>.stdout.log``/``<task_id>.stderr.log`` ficam em
    ``<root>/.progress/`` e serão consumidos pelo ``/v1/progress/{task_id}`` para
    snapshot mid-flight no painel TUI. Em timeout, devolvemos ``returncode=124``
    (convenção do ``coreutils timeout``) com mensagem em ``stderr``.

    Quando ``lease_path`` é informado, gravamos o PID do subprocess no lease
    assim que ele é spawnado e limpamos ao terminar — isso permite que um
    observador (painel, ``kubectl exec``) distinga "lease vivo por heartbeat" de
    "CLI rodando agora" sem ter que varrer ``/proc`` (o lease é mantido vivo pela
    heartbeat task do servidor mesmo quando o subprocess já terminou).

    ``root`` define onde gravar os arquivos de progresso; quando omitido, cai em
    ``DEILE_CLAUDE_WORKER_ROOT`` (default ``/home/claude/work``) por compat com o
    claude-worker.
    """
    start = time.monotonic()

    # Persistir progress files em <root>/.progress/.
    if root is None:
        root = Path(
            os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work")
        )
    progress_dir = root / ".progress"
    progress_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = progress_dir / f"{task_id}.stdout.log"
    stderr_path = progress_dir / f"{task_id}.stderr.log"

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if lease_path is not None:
        await update_lease_subprocess_pid(lease_path, proc.pid)

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        if lease_path is not None:
            await update_lease_subprocess_pid(lease_path, None)
        duration = time.monotonic() - start
        return SubprocessResult(
            returncode=124, stdout="",
            stderr=f"subprocess timed out after {timeout}s",
            duration_seconds=duration,
        )

    duration = time.monotonic() - start
    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")

    if lease_path is not None:
        await update_lease_subprocess_pid(lease_path, None)

    # Persiste para o ``/v1/progress`` — best-effort; falha em escrita NÃO
    # derruba o dispatch (o cliente já recebeu o resultado).
    try:
        await asyncio.to_thread(stdout_path.write_text, stdout)
        await asyncio.to_thread(stderr_path.write_text, stderr)
    except OSError as exc:
        logger.warning(
            "failed to persist progress logs for task_id=%s: %s", task_id, exc,
        )

    return SubprocessResult(
        returncode=proc.returncode or 0,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
    )


# --------------------------------------------------------------------------- #
# HTTP — Bearer auth middleware + rate limiter por ator
# --------------------------------------------------------------------------- #


def make_bearer_auth_mw(whitelist: Iterable[str]):
    """Cria um middleware aiohttp de Bearer auth com *whitelist* de paths abertos.

    Os paths em *whitelist* (ex.: ``/v1/health``) dispensam token. Demais paths
    exigem ``Authorization: Bearer <token>`` comparado em constant-time
    (``hmac.compare_digest``) contra ``request.app["auth_token"]`` para evitar
    timing-attack na descoberta.
    """
    open_paths = frozenset(whitelist)

    @web.middleware
    async def _mw(request: web.Request, handler):
        if request.path in open_paths:
            return await handler(request)
        expected = request.app["auth_token"]
        got = request.headers.get("Authorization", "")
        if not got.startswith("Bearer ") or not hmac.compare_digest(
                got[len("Bearer "):], expected):
            return web.json_response(
                {"error": {"code": "UNAUTHORIZED", "message": "bad bearer"}},
                status=401,
            )
        return await handler(request)

    return _mw


@dataclass
class RateLimiter:
    """Rate-limiter por ator: sliding-window in-memory thread-safe.

    Usado para gatekeep de endpoints sensíveis (ex.: acesso raw a prompts).
    ``max_requests`` por ``window_s`` segundos, por ator.
    """

    max_requests: int = 10
    window_s: float = 60.0

    def __post_init__(self) -> None:
        # {actor: [timestamp, ...]} — monotonic times of recent requests.
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def check(self, actor: str) -> bool:
        """Return True if *actor* is within rate limit, False if exceeded.

        Slides a window over recent request timestamps. Thread-safe (the lock is
        held only for dict mutation).
        """
        now = time.monotonic()
        cutoff = now - self.window_s
        with self._lock:
            bucket = self._buckets.get(actor, [])
            # Prune timestamps outside the window.
            bucket = [t for t in bucket if t > cutoff]
            if len(bucket) >= self.max_requests:
                self._buckets[actor] = bucket
                return False
            bucket.append(now)
            self._buckets[actor] = bucket
            return True


__all__ = [
    "TASK_ID_RE",
    "DEFAULT_LEASE_TTL_S",
    "DEFAULT_LEASE_HEARTBEAT_S",
    "validate_task_id_for_path",
    "pid_alive",
    "dir_bytes",
    "acquire_lease",
    "update_lease_subprocess_pid",
    "release_lease",
    "heartbeat_loop",
    "lease_is_stale",
    "SubprocessResult",
    "run_subprocess_with_progress",
    "make_bearer_auth_mw",
    "RateLimiter",
]
