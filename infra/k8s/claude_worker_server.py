#!/usr/bin/env python3
"""claude_worker_server — long-running ``claude-worker`` Pod (issue #309 fase 2).

Servidor HTTP aiohttp dentro do Pod ``claude-worker``. Recebe dispatches do
``deile-pipeline`` (Bearer auth, escopo do mesmo secret do ``deile-worker``)
e executa ``claude -p`` em subprocess sob ``/home/claude/work/<task_id>/``.
Diferenças de papel vs. o ``deile-worker``:

* O ``deile-worker`` roda o agente DEILE in-process e usa provedores LLM via
  ``*_API_KEY``. O ``claude-worker`` NÃO carrega API keys — o ``claude`` CLI
  usa autenticação por assinatura do Claude Code; ``ANTHROPIC_API_KEY`` é
  explicitamente removido pelo wrapper antes deste módulo subir.
* A allowlist regex de repositórios (``/etc/claude-worker/allowed_repos.regex``)
  é montada pelo wrapper e usada para barrar ``git push`` para destinos
  arbitrários (defense-in-depth contra prompt-injection no brief).

Endpoints:

* ``GET  /v1/health``              — readiness/liveness probe (Task 12)
* ``GET  /v1/pod-status``          — pod introspection: lease/disk/GC/quota (issue #395)
* ``POST /v1/dispatch``            — receive brief + spawn ``claude -p`` (Task 13)
* ``GET  /v1/progress/{task_id}``  — mid-flight snapshot via PVC tail (Task 14)

Spec: ``docs/superpowers/specs/2026-05-26-claude-worker-design.md`` §4.4.
"""

from __future__ import annotations

import asyncio
import fcntl
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from aiohttp import web

import dispatch_logger as dlog

logger = logging.getLogger("deile.claude_worker_server")

#: ``secrets.token_hex(8)`` gera exatamente 16 chars hex; qualquer outra
#: forma é rejeitada para não permitir path traversal pela URL nem leitura
#: de arquivos arbitrários no PVC. Definida aqui (antes de qualquer uso)
#: porque os mecanismos de lease (seção abaixo) referenciam este padrão.
_TASK_ID_RE = re.compile(r"[0-9a-f]{16}")


# --------------------------------------------------------------------------- #
# Mecanismo 1 — OAuth file-lock cross-pod
#
# Quando o claude-worker tem N réplicas, todas elas montam o mesmo PVC
# (``claude-worker-home``) com o mesmo ``credentials.json``. Sem lock, dois
# pods que detectam expiração simultânea disparam refresh concorrente e o
# segundo write corrompe o token recém-gravado pelo primeiro. O flock garante
# serialização: apenas um pod escreve de cada vez; os demais aguardam e então
# leem o token já atualizado.
# --------------------------------------------------------------------------- #


def _creds_path() -> Path:
    """Caminho canônico do credentials.json (montado pelo initContainer)."""
    home = Path(os.environ.get("HOME", "/home/claude"))
    return home / ".claude" / "credentials.json"


def _is_expiring_soon(creds: dict, window_s: int = 300) -> bool:
    """True se o ``accessToken`` expira nos próximos *window_s* segundos.

    O campo ``expiresAt`` segue o formato do Claude Code: inteiro de
    milissegundos de epoch (ms). Ausente ou inválido → assume que NÃO
    expira em breve (fail-open: não dispara refresh desnecessário).
    """
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    expires_at_ms = (oauth or {}).get("expiresAt") if isinstance(oauth, dict) else None
    if expires_at_ms is None:
        expires_at_ms = creds.get("expiresAt") if isinstance(creds, dict) else None
    if not isinstance(expires_at_ms, (int, float)):
        return False
    expires_at_s = float(expires_at_ms) / 1000.0
    return (expires_at_s - time.time()) < window_s


# mtime do credentials.json na última leitura bem-sucedida — permite
# detectar quando outro pod escreveu um token novo sem ter que relê-lo
# incondicionalmente a cada dispatch.
_creds_last_mtime: float = 0.0


def _refresh_oauth_with_lock(creds_path: Optional[Path] = None) -> bool:
    """Lê ``credentials.json`` sob lock exclusivo e atualiza ``ANTHROPIC_AUTH_TOKEN``.

    Sequência:
    1. Abre o arquivo em modo ``r+`` (leitura+escrita, não trunca).
    2. Adquire ``LOCK_EX`` (bloqueia até o lock ser obtido — outros pods
       que chegarem aqui ficam em espera).
    3. Relê o conteúdo (pode ter mudado desde o open, pois outro pod pode
       ter acabado de escrever).
    4. Verifica ``expiresAt``; se expirando em <5 min, loga aviso (refresh
       real via ``claude`` CLI não é tentado — o ``claude -p`` subprocess
       faz o refresh in-place quando necessário). O lock garante que apenas
       um pod detecta/age sobre a expiração de cada vez.
    5. Exporta o token mais fresco como ``ANTHROPIC_AUTH_TOKEN``.
    6. Libera o lock ao sair do ``with`` (``LOCK_UN`` automático no close).

    Best-effort: erros de I/O ou parse viram ``logger.warning`` e a função
    retorna ``False`` — o dispatch continua com o token anterior (se havia
    um carregado no startup), que pode ser válido ainda.

    Returns:
        ``True`` se ``ANTHROPIC_AUTH_TOKEN`` foi (re)carregado com sucesso.
    """
    global _creds_last_mtime  # noqa: PLW0603
    if creds_path is None:
        creds_path = _creds_path()
    if not creds_path.exists():
        logger.debug("credentials.json não encontrado em %s — skip refresh", creds_path)
        return False
    try:
        current_mtime = creds_path.stat().st_mtime
    except OSError as exc:
        logger.warning("stat(%s) falhou: %s", creds_path, exc)
        return False
    # Evita releitura desnecessária quando mtime não mudou (arquivo idêntico
    # ao último load). O flock é obtido mesmo assim para serializar quaisquer
    # concurrent refreshes que possam estar em voo.
    try:
        with open(creds_path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read()
            finally:
                # Liberação explícita antes do close para minimizar a janela
                # de lock ao usar o token (não precisamos do lock durante o
                # os.environ write — é local ao processo).
                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("flock/read em %s falhou: %s", creds_path, exc)
        return False

    try:
        creds = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("credentials.json malformado em %s: %s", creds_path, exc)
        return False

    if _is_expiring_soon(creds):
        logger.warning(
            "ANTHROPIC_AUTH_TOKEN está expirando em breve em %s — "
            "rode `deploy.py k8s claude-renew` para renovar",
            creds_path,
        )

    # Extrai token — mesma lógica de _load_oauth_token_into_env.
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    token = (oauth or {}).get("accessToken") if isinstance(oauth, dict) else None
    if not token:
        token = creds.get("accessToken") if isinstance(creds, dict) else None
    if not token:
        logger.warning(
            "credentials.json não contém accessToken em %s — "
            "claude CLI vai reportar 'Not logged in'",
            creds_path,
        )
        return False

    os.environ["ANTHROPIC_AUTH_TOKEN"] = token
    _creds_last_mtime = current_mtime
    logger.debug(
        "ANTHROPIC_AUTH_TOKEN (re)carregado de %s (len=%d, mtime=%.0f)",
        creds_path, len(token), current_mtime,
    )
    return True


# --------------------------------------------------------------------------- #
# Mecanismo 2 — Lease por task_id (filesystem-based, atomic)
#
# Cada task possui um arquivo ``.lease.json`` dentro do seu workspace
# (``<root>/<task_id>/.lease.json``). O lease identifica qual pod/pid está
# trabalhando e quando foi o último heartbeat. A aquisição é atômica via
# write-tmp + rename (POSIX garantia de atomicidade). O heartbeat é atualizado
# periodicamente por uma asyncio.Task; quando ela para (pod morreu, processo
# travou), o arquivo fica desatualizado e o próximo pod que tentar adquirir
# trata o workspace como disponível após o TTL expirar.
# --------------------------------------------------------------------------- #

#: TTL em segundos — lease considera-se morto se ``heartbeat_at`` for
#: mais antigo que este valor. Configurável via env para ajuste operacional.
_LEASE_TTL_S: int = int(os.environ.get("DEILE_CLAUDE_LEASE_TTL_S", "30"))

#: Intervalo de atualização do heartbeat em segundos.
_LEASE_HEARTBEAT_S: int = int(os.environ.get("DEILE_CLAUDE_LEASE_HEARTBEAT_S", "5"))


# --------------------------------------------------------------------------- #
# Anthropic quota cache — thread-safe singleton (issue #395)
#
# Populated best-effort when dispatch output contains rate-limit header
# patterns.  Never makes additional API calls — returns None when nothing
# has been captured yet.
# --------------------------------------------------------------------------- #

@dataclass
class _QuotaSnapshot:
    tokens_remaining: int
    captured_at: float


_quota_lock: threading.Lock = threading.Lock()
_quota_snapshot: Optional[_QuotaSnapshot] = None

#: Regex pattern matching anthropic rate-limit header in subprocess output.
_QUOTA_RE = re.compile(
    r"(?:anthropic-ratelimit-tokens-remaining|x-ratelimit-remaining-tokens)"
    r"[:\s]+(\d+)",
    re.IGNORECASE,
)


def _update_quota_cache(tokens_remaining: int) -> None:
    global _quota_snapshot  # noqa: PLW0603
    with _quota_lock:
        _quota_snapshot = _QuotaSnapshot(
            tokens_remaining=tokens_remaining,
            captured_at=time.time(),
        )


def _get_quota_snapshot() -> Optional[_QuotaSnapshot]:
    with _quota_lock:
        return _quota_snapshot


def _try_capture_quota_from_output(stdout: str, stderr: str) -> None:
    """Best-effort scan of subprocess output for rate-limit token counts."""
    for text in (stderr, stdout):
        m = _QUOTA_RE.search(text)
        if m:
            try:
                _update_quota_cache(int(m.group(1)))
                return
            except ValueError:
                pass


def _validate_task_id_for_path(task_id: str) -> bool:
    """Defesa contra path traversal: task_id deve ser hex 16-char."""
    return bool(_TASK_ID_RE.fullmatch(task_id)) if task_id else False


async def _acquire_lease(workspace: Path) -> Optional[dict]:
    """Tenta adquirir o lease do workspace.

    Algoritmo:
    1. Se ``.lease.json`` existir e ``heartbeat_at`` for recente (< TTL),
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
            return heartbeat_age < _LEASE_TTL_S
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


async def _release_lease(lease_path: Path) -> None:
    """Remove o arquivo de lease. Idempotente: FileNotFoundError é ignorado."""
    def _unlink() -> None:
        try:
            lease_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("falha ao remover lease %s: %s", lease_path, exc)

    await asyncio.to_thread(_unlink)


async def _heartbeat_loop(lease_path: Path, stop_event: asyncio.Event) -> None:
    """Atualiza ``heartbeat_at`` no lease a cada ``_LEASE_HEARTBEAT_S`` segundos.

    Best-effort: erros de I/O são logados e ignorados — o heartbeat pode
    deixar de ser atualizado sem derrubar o dispatch. Se o pod perder acesso
    ao PVC (improvável em k3s single-node), o pipeline detectará o TTL
    expirado e tentará adquirir o lease no próximo tick.

    A task é cancelada (ou ``stop_event`` é setado) quando o dispatch terminar.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(_LEASE_HEARTBEAT_S))
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        def _update() -> None:
            try:
                content = json.loads(lease_path.read_text(encoding="utf-8"))
                content["heartbeat_at"] = time.time()
                # Write atômico para não corromper o lease se o processo
                # for interrompido no meio da escrita.
                tmp = lease_path.with_suffix(".json.hb_tmp")
                tmp.write_text(json.dumps(content), encoding="utf-8")
                tmp.rename(lease_path)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("heartbeat update falhou em %s: %s", lease_path, exc)

        await asyncio.to_thread(_update)


# --------------------------------------------------------------------------- #
# OAuth token extraction — claude CLI no Linux NÃO lê
# ``~/.claude/credentials.json`` automaticamente (esse caminho é uma
# convenção macOS — no Linux ele só lê variáveis de ambiente). Extraímos
# o ``accessToken`` no startup e o exportamos como ``ANTHROPIC_AUTH_TOKEN``
# antes de spawnar o subprocess do claude.
# --------------------------------------------------------------------------- #


def _load_oauth_token_into_env() -> bool:
    """Lê ``credentials.json`` (mountado pelo initContainer) e exporta
    ``ANTHROPIC_AUTH_TOKEN`` na env do processo.

    Returns ``True`` se token foi carregado; ``False`` caso contrário (file
    ausente, JSON malformado, sem ``claudeAiOauth.accessToken``). O server
    continua subindo em qualquer caso — a falha real aparece quando o
    ``claude -p`` rodar e reportar ``Not logged in``.
    """
    home = Path(os.environ.get("HOME", "/home/claude"))
    creds_path = home / ".claude" / "credentials.json"
    if not creds_path.exists():
        logger.warning("credentials.json não encontrado em %s", creds_path)
        return False
    try:
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("não foi possível parsear %s: %s", creds_path, exc)
        return False
    # macOS Keychain JSON: {"claudeAiOauth": {"accessToken": "..."}}
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    token = (oauth or {}).get("accessToken") if isinstance(oauth, dict) else None
    if not token:
        # Fallback: tenta "accessToken" no root level (formatos diferentes).
        token = creds.get("accessToken") if isinstance(creds, dict) else None
    if not token:
        logger.warning(
            "credentials.json não contém claudeAiOauth.accessToken nem "
            "accessToken root-level — claude CLI vai reportar 'Not logged in'",
        )
        return False
    os.environ["ANTHROPIC_AUTH_TOKEN"] = token
    logger.info("ANTHROPIC_AUTH_TOKEN carregado de %s (len=%d)",
                creds_path, len(token))
    return True


# --------------------------------------------------------------------------- #
# Startup cleanup (issue #408) — varre o PVC de trabalho, remove leases
# stale e workdirs abandonados antes do server aceitar conexões.
# --------------------------------------------------------------------------- #

#: Retenção padrão em dias — workdirs sem atividade mais antigos que isso
#: são removidos. Configurável via env para ambientes com retenção maior.
_CLEANUP_RETENTION_DAYS: int = int(
    os.environ.get("DEILE_CLAUDE_CLEANUP_RETENTION_DAYS", "7")
)


def _pid_alive(pid: int) -> bool:
    """True se o PID ainda está vivo neste sistema (POSIX)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _lease_is_stale(lease_path: Path) -> bool:
    """True se o lease expirou E o PID proprietário não está mais vivo.

    Conservador: se não conseguir ler o lease, assume que NÃO é stale
    (fail-safe — evita apagar workdirs em uso quando o FS está lento).
    """
    try:
        data = json.loads(lease_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    heartbeat_at = data.get("heartbeat_at", 0)
    if (time.time() - float(heartbeat_at)) < _LEASE_TTL_S:
        return False  # heartbeat recente → ativo
    pid = data.get("pid")
    if pid and _pid_alive(int(pid)):
        return False  # TTL expirado mas PID ainda vivo → conservador
    return True


def _workdir_has_session(workdir: Path) -> bool:
    """True se existe um JSONL de sessão claude para este workdir.

    Claude armazena sessões em ``~/.claude/projects/-home-claude-work-<task_id>/``,
    não dentro do próprio workdir.
    """
    task_id = workdir.name
    home = Path(os.environ.get("HOME", "/home/claude"))
    workspace_hash = "-home-claude-work-" + task_id
    project_dir = home / ".claude" / "projects" / workspace_hash
    if not project_dir.is_dir():
        return False
    try:
        return any(project_dir.glob("*.jsonl"))
    except OSError:
        return False


def _dir_bytes(path: Path) -> int:
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


def startup_cleanup(root: Optional[Path] = None) -> dict:
    """Remove leases stale e workdirs abandonados do PVC de trabalho.

    Chamada de forma síncrona durante o boot (antes de :func:`web.run_app`),
    idempotente e conservadora: nunca remove workdirs com lease ativo ou
    modificados recentemente.

    Returns:
        dict com campos ``leases_removed``, ``workdirs_removed``,
        ``bytes_freed`` e ``errors`` para o audit log.
    """
    if root is None:
        root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    retention_cutoff = time.time() - (_CLEANUP_RETENTION_DAYS * 86400)

    leases_removed: int = 0
    workdirs_removed: int = 0
    bytes_freed: int = 0
    errors: list = []

    if not root.is_dir():
        return {
            "leases_removed": 0,
            "workdirs_removed": 0,
            "bytes_freed": 0,
            "errors": ["work root not found"],
        }

    try:
        candidates = [
            d for d in root.iterdir()
            if d.is_dir() and _TASK_ID_RE.fullmatch(d.name)
        ]
    except OSError as exc:
        return {
            "leases_removed": 0,
            "workdirs_removed": 0,
            "bytes_freed": 0,
            "errors": [f"cannot list work root: {exc}"],
        }

    for workdir in candidates:
        lease_path = workdir / ".lease.json"

        # Lease vivo → workdir em uso, pula completamente.
        if lease_path.exists() and not _lease_is_stale(lease_path):
            continue

        # Lease stale → remove só o arquivo de lease (workdir pode ter dados úteis).
        if lease_path.exists() and _lease_is_stale(lease_path):
            try:
                lease_path.unlink()
                leases_removed += 1
                logger.info("startup_cleanup: lease stale removido: %s", lease_path)
            except OSError as exc:
                errors.append(f"lease unlink {lease_path}: {exc}")

        # Critério de remoção do workdir inteiro:
        # 1. Sem sessão JSONL (claude nunca rodou — workdir órfão de alocação).
        # 2. last_modified anterior ao cutoff de retenção.
        try:
            last_mod = workdir.stat().st_mtime
        except OSError as exc:
            errors.append(f"stat {workdir}: {exc}")
            continue

        remove_reason: Optional[str] = None
        if not _workdir_has_session(workdir):
            remove_reason = "no session JSONL"
        elif last_mod < retention_cutoff:
            remove_reason = f"older than {_CLEANUP_RETENTION_DAYS}d"

        if remove_reason:
            size = _dir_bytes(workdir)
            try:
                shutil.rmtree(workdir)
                workdirs_removed += 1
                bytes_freed += size
                logger.info(
                    "startup_cleanup: workdir removido (%s): %s (%d bytes)",
                    remove_reason, workdir, size,
                )
            except OSError as exc:
                errors.append(f"rmtree {workdir}: {exc}")

    logger.info(
        "startup_cleanup concluído: leases=%d workdirs=%d freed=%d bytes errors=%d",
        leases_removed, workdirs_removed, bytes_freed, len(errors),
    )
    for err in errors:
        logger.warning("startup_cleanup error: %s", err)

    return {
        "leases_removed": leases_removed,
        "workdirs_removed": workdirs_removed,
        "bytes_freed": bytes_freed,
        "errors": errors,
    }


# --------------------------------------------------------------------------- #
# Bearer auth (defense-in-depth — NetworkPolicy bloqueia ingress fora do
# deile-pipeline, mas auth no app-layer impede que pod comprometido dentro
# do allowlist envie dispatch malicioso).
# --------------------------------------------------------------------------- #


def _read_auth_token() -> str:
    """Lê o Bearer token do Secret K8s ``claude-worker-bearer``.

    Caminhos em ordem (primeiro existente vence):
    1. ``/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN`` (Secret
       montado como file pelo manifest 50).
    2. ``DEILE_CLAUDE_WORKER_AUTH_TOKEN_FILE`` env var (override pra dev).
    3. ``DEILE_CLAUDE_WORKER_AUTH_TOKEN`` env var (testes apenas — nunca
       loga o valor).

    Raises:
        RuntimeError: nenhuma source disponível (Secret não populado +
            env vars vazias) — server abort no startup pra forçar fix.
    """
    candidates = [
        Path("/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN"),
        Path(os.environ.get("DEILE_CLAUDE_WORKER_AUTH_TOKEN_FILE", "")),
    ]
    for p in candidates:
        if p and p.is_file():
            token = p.read_text(encoding="utf-8").strip()
            if token:
                return token
    env_val = os.environ.get("DEILE_CLAUDE_WORKER_AUTH_TOKEN", "").strip()
    if env_val:
        return env_val
    raise RuntimeError(
        "claude-worker auth token not found: expected "
        "/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN "
        "(populated by deploy.py k8s claude-login) or "
        "DEILE_CLAUDE_WORKER_AUTH_TOKEN env"
    )


@web.middleware
async def _bearer_auth_mw(request: web.Request, handler):
    """Bearer auth middleware (paridade com ``worker_server._bearer_auth_mw``).

    Whitelist ``/v1/health`` (readiness probe sem token). Demais paths
    exigem ``Authorization: Bearer <token>`` comparado em constant-time
    (``hmac.compare_digest``) para evitar timing-attack na descoberta.
    """
    if request.path in ("/v1/health", "/v1/auth/start", "/v1/auth/status"):
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


# --------------------------------------------------------------------------- #
# Subprocess execution
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


#: Preambles por stage. Cada um descreve identidade + contrato de output, com
#: placeholders ``$BRANCH``/``$TASK_ID`` substituídos por
#: :func:`_render_preamble` antes do exec.
PREAMBLE_TEMPLATES = {
    "implement": (
        "Você é Claude Code em modo autônomo (claude-worker pod, dispatch local).\n"
        "Worktree: já checked out em $PWD, branch $BRANCH.\n"
        "Tarefa: implemente o que está descrito após '---' abaixo.\n"
        "Quando terminar com sucesso, imprima 'STATUS: SUCCESS' como última linha.\n"
        "Em falha, 'STATUS: BLOCKED_<motivo>'.\n"
        "NÃO faça merge, NÃO use push --force, NÃO use --no-verify."
    ),
    "review": (
        "Você é Claude Code revisor (claude-worker pod). Worktree: $PWD, branch $BRANCH.\n"
        "Tarefa: revise a PR descrita após '---'. Comente achados via gh CLI.\n"
        "Imprima 'STATUS: SUCCESS' quando review estiver postado; "
        "'STATUS: BLOCKED_<motivo>' em falha."
    ),
    "classify": (
        "Você é Claude Code classificador (claude-worker pod). Tarefa: classifique "
        "a issue descrita após '---'. Imprima JSON com {category, severity, "
        "estimated_effort}. 'STATUS: SUCCESS' ao final."
    ),
    "refine": (
        "Você é Claude Code refinador (claude-worker pod). Tarefa: refine o body "
        "da issue descrita após '---' editando-a via gh CLI. 'STATUS: SUCCESS' ao final."
    ),
    "pr_review": (
        "Você é Claude Code revisor de PR (claude-worker pod). Worktree: $PWD, "
        "branch $BRANCH. Revise rigorosamente a PR descrita após '---'.\n"
        "\n"
        "REGRA OBRIGATÓRIA (não negociável): a EXECUÇÃO INTEIRA é considerada "
        "FALHA se você terminar sem ter postado pelo menos um destes:\n"
        "  - `gh pr review <pr_number> --comment --body \"<resumo>\"` (top-level), OU\n"
        "  - `gh api repos/<owner>/<repo>/pulls/<pr>/comments -f body=...` (inline), OU\n"
        "  - `gh issue comment <pr_number> --body \"<resumo>\"` (fallback simples)\n"
        "\n"
        "Não basta analisar e imprimir STATUS — o operador precisa VER a review "
        "no GitHub. Faça primeiro o `gh pr review` (ou `gh issue comment`), CONFIRME "
        "que postou (saída do comando contém URL), e SÓ ENTÃO imprima 'STATUS: APPROVE' "
        "ou 'STATUS: REQUEST_CHANGES'. Em bloqueio real: imprima "
        "'STATUS: BLOCKED_<motivo>' DEPOIS de também postar um `gh issue comment` "
        "explicando o que faltou."
    ),
    "follow_ups": (
        "Você é Claude Code follow-up handler (claude-worker pod). Worktree: $PWD. "
        "Trate os follow-ups descritos após '---'. 'STATUS: SUCCESS' ao final."
    ),
}


def _render_preamble(stage: str, branch: Optional[str], task_id: str) -> str:
    """Renderiza o preamble por ``stage`` substituindo placeholders.

    Stage desconhecido cai no template ``implement`` (default seguro: pede
    ``STATUS: SUCCESS`` e desencoraja operações destrutivas). ``$PWD`` fica
    vazio — o ``claude`` descobre via ``pwd`` na sessão; usamos a string só
    para sinalizar ao agente que ele já está no diretório certo.
    """
    template = PREAMBLE_TEMPLATES.get(stage, PREAMBLE_TEMPLATES["implement"])
    return (
        template
        .replace("$BRANCH", branch or "(no branch)")
        .replace("$PWD", "")
        .replace("$TASK_ID", task_id)
    )


async def run_subprocess_with_progress(
    args: list,
    *,
    cwd: Path,
    task_id: str,
    timeout: int,
) -> SubprocessResult:
    """Spawn de ``claude -p`` com persistência de stdout/stderr para o PVC.

    Os arquivos ``<task_id>.stdout.log``/``<task_id>.stderr.log`` ficam em
    ``DEILE_CLAUDE_WORKER_ROOT/.progress/`` e serão consumidos pelo
    ``/v1/progress/{task_id}`` (Task 14) para snapshot mid-flight no painel
    TUI. Em timeout, devolvemos ``returncode=124`` (convenção do ``coreutils
    timeout``) com mensagem em ``stderr``.
    """
    start = time.monotonic()

    # Persistir progress files em DEILE_CLAUDE_WORKER_ROOT/.progress/.
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
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

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        duration = time.monotonic() - start
        return SubprocessResult(
            returncode=124, stdout="",
            stderr=f"claude -p timed out after {timeout}s",
            duration_seconds=duration,
        )

    duration = time.monotonic() - start
    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")

    # Persiste para o ``/v1/progress`` (Task 14) — best-effort; falha em
    # escrita NÃO derruba o dispatch (o cliente já recebeu o resultado).
    try:
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
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


#: Slugs internos do DEILE têm forma ``provider:model``. O ``claude-worker``
#: só aceita ``anthropic:*`` — outros providers são rejeitados em 400.
_ANTHROPIC_SLUG_RE = re.compile(r"^anthropic:(.+)$")

#: Níveis que o ``claude`` CLI aceita via ``--effort`` (print mode) — verificado
#: empiricamente contra o binário: ``low|medium|high|xhigh|max`` (qualquer outro
#: valor faz o commander sair com erro ANTES de qualquer chamada de API). NÃO
#: inclui ``ultracode``/``auto``: esses são do vocabulário interativo (slash
#: ``/effort``) e o flag ``--effort`` os REJEITA. O vocabulário Claude Code
#: completo (com ultracode/auto) vive em ``CLAUDE_CODE_EFFORTS`` no pacote
#: ``deile`` — aqui só os aceitos pelo CLI. Tudo ``[a-z]`` puro (sem metacaractere
#: de shell no argv). :func:`_coerce_claude_effort` traduz ultracode→xhigh e
#: auto→(omitir) antes de montar o argv.
_VALID_CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def _coerce_claude_effort(raw: Optional[str]) -> Optional[str]:
    """Traduz um nível de reasoning para um valor aceito por ``claude --effort``.

    - ``None``/vazio/``auto`` → ``None`` (omite o flag; claude usa o default).
    - ``ultracode`` → ``xhigh``: ultracode = xhigh + palavra "workflow" no prompt
      (modo interativo); em ``-p`` replicamos só o esforço — ``xhigh`` é o nível
      correto (``max`` é um tier acima separado).
    - ``low|medium|high|xhigh|max`` → passa direto.
    - qualquer outro (ex.: ``off``/``none``/``minimal`` de uma config trocada) →
      ``None`` com warning (fail-open; nunca passa valor inválido pro argv, o que
      faria ``claude -p`` sair com erro e a dispatch falhar 100%).
    """
    if raw is None:
        return None
    v = str(raw).strip().lower()
    if not v or v == "auto":
        return None
    if v == "ultracode":
        return "xhigh"
    if v in _VALID_CLAUDE_EFFORTS:
        return v
    logger.warning(
        "claude-worker: effort %r não é aceito por 'claude --effort' "
        "(esperado %s) — omitido", v, sorted(_VALID_CLAUDE_EFFORTS),
    )
    return None


def _is_ultracode(raw: Optional[str]) -> bool:
    """``True`` quando o effort pedido é exatamente ``ultracode``.

    Ultracode no Claude Code = ``xhigh`` + orquestração dynamic-workflow "de
    pé". O ``--effort`` cobre o ``xhigh`` (via :func:`_coerce_claude_effort`);
    a segunda metade — opt-in no Workflow tool — não tem flag de CLI: o binário
    detecta o keyword ``workflow``/``workflows`` no prompt. Esta função sinaliza
    quando injetar :data:`_ULTRACODE_PREAMBLE` para reproduzir o preset em ``-p``.
    """
    return raw is not None and str(raw).strip().lower() == "ultracode"


#: Preâmbulo injetado no prompt quando o effort pedido é ``ultracode``. Carrega
#: o keyword ``workflow`` que o ``claude`` CLI usa para opt-in no Workflow tool
#: (orquestração multi-agente). Requer Dynamic Workflows habilitado na conta
#: OAuth do pod — se estiver off, o CLI ignora o keyword (sem quebrar o
#: dispatch). Para edições triviais o agente segue direto; o fan-out só vale
#: quando a tarefa se beneficia.
_ULTRACODE_PREAMBLE = (
    "ULTRACODE: para tarefas substanciais, orquestre via workflow multi-agente "
    "(decomponha o problema, paralelize as frentes independentes e verifique "
    "adversarialmente os achados antes de concluir) em vez de resolver tudo "
    "numa única linha de raciocínio. Para edições triviais ou mecânicas, siga "
    "direto sem fan-out.\n\n---\n\n"
)


# --------------------------------------------------------------------------- #
# Session metadata persistence (issue #309 fase 3.5 — resume support)
# --------------------------------------------------------------------------- #
#
# Cada dispatch fixa um session-id UUID4 que é passado ao claude CLI via
# ``--session-id``. claude grava a conversa em
# ``~/.claude/projects/-home-claude-work-<task_id>/<session-id>.jsonl`` e
# aceita retomada via ``-r <session-id>``. Persistimos o session-id + o
# workdir + status final em ``~/.claude/tasks/<task_id>/session.json``
# para que o pipeline possa orquestrar resume via novo endpoint
# ``GET /v1/dispatches/{task_id}/resume-info``.
#
# Estrutura do session.json:
#   {
#     "task_id": "abc123...",                       # hex 16 (mesmo do dispatch)
#     "session_id": "uuid4-aaaa-bbbb-cccc",         # passado ao claude
#     "workdir": "/home/claude/work/abc123...",     # cwd do spawn
#     "stage": "pr_review",                         # do payload
#     "branch": "auto/issue-N",                     # do payload (opt)
#     "model": "claude-sonnet-4-6",                 # do payload (opt)
#     "started_at": 1716830000,                     # unix ts (created)
#     "last_completed_at": 1716830420,              # unix ts (last exit)
#     "last_is_error": false,                       # do JSON output do claude
#     "last_result_summary": "Review postada...",   # first 300 chars do result
#     "last_returncode": 0,                         # exit code do claude
#     "last_duration_seconds": 420.5,               # do SubprocessResult
#     "last_total_cost_usd": 0.137,                 # do JSON output do claude
#     "prev_task_id": "xyz...",                     # se este dispatch foi resume
#     "attempt": 2,                                 # 1 no fresh; +1 por resume
#   }


def _session_meta_dir() -> Path:
    return Path(os.environ.get("HOME", "/home/claude")) / ".claude" / "tasks"


def _session_meta_path(task_id: str) -> Path:
    return _session_meta_dir() / task_id / "session.json"


def _save_session_meta(task_id: str, meta: dict) -> None:
    """Persiste atomicamente o session.json (write-tmp + replace).

    Best-effort: falha de I/O vira logger.warning, NÃO derruba o dispatch
    (o cliente já recebeu o resultado). Atomicidade evita meta corrompido
    se o pod morrer no meio da escrita — pipeline lê estado consistente.
    """
    path = _session_meta_path(task_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("failed to write session meta for task_id=%s: %s",
                       task_id, exc)


def _load_session_meta(task_id: str) -> Optional[dict]:
    """Carrega session.json. None se ausente, malformado, ou I/O error."""
    path = _session_meta_path(task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("failed to read session meta for task_id=%s: %s",
                       task_id, exc)
        return None


#: Default /proc root pra detecção de processo claude vivo. Testes
#: monkeypatcham essa variável apontando pra fake dir.
_PROC_ROOT: str = "/proc"


#: Janela em segundos pra considerar uma sessão claude "viva" via mtime do JSONL.
#: 60s cobre o caso de claude levando até 1 turno completo (incluindo tools)
#: sem appendar — append típico em pytest run é <30s entre turns.
_JSONL_ALIVE_THRESHOLD_S: int = 60


def _is_session_jsonl_recently_active(
    session_id: str, threshold_s: int = _JSONL_ALIVE_THRESHOLD_S,
) -> bool:
    """True se o JSONL da sessão claude foi modificado nos últimos
    ``threshold_s`` segundos.

    O JSONL vive em ``~/.claude/projects/-<workspace_hash>/<session_id>.jsonl``
    no **PVC compartilhado** entre réplicas claude-worker. claude appenda
    toda vez que recebe um turn — então se mtime é recente, a sessão
    está VIVA mesmo que o processo não esteja visível neste ``/proc``
    local (ex.: rodando em outra réplica do StatefulSet/Deployment).

    Este check é o complemento multi-replica safe do scan via ``/proc``
    (``_find_claude_pid``); juntos blindam contra triple-dispatch que
    ocorria quando o Service ``claude-worker:8767`` distribuía
    ``resume-info`` round-robin entre pods, e o pod que recebia a query
    não enxergava o processo vivo no /proc do pod onde claude girava.

    Best-effort: erros viram False (fail-open, igual ao caminho /proc).
    """
    if not session_id:
        return False
    try:
        home = Path(os.environ.get("HOME", "/home/claude"))
        projects_dir = home / ".claude" / "projects"
        if not projects_dir.is_dir():
            return False
        cutoff = time.time() - threshold_s
        for sub in projects_dir.iterdir():
            if not sub.is_dir():
                continue
            jsonl = sub / f"{session_id}.jsonl"
            try:
                if jsonl.exists() and jsonl.stat().st_mtime > cutoff:
                    return True
            except OSError:
                continue
        return False
    except OSError:
        return False


def _is_claude_process_alive(session_id: str) -> bool:
    """True se o session_id ainda está em execução ativa.

    Liveness via três sinais em ordem de prioridade:

    1. **Lease** (mecanismo 3, prioritário): lê ``workdir/.lease.json``
       via reverse-lookup do session_id. Se o heartbeat estiver fresco
       (< TTL), a task está ativa em alguma réplica — resposta definitiva
       cross-pod sem depender de ``/proc`` local.
    2. **``/proc`` local** (fallback rápido): cobre dispatches pré-lease
       (pods rodando versão anterior) e contextos de teste/dev fora do
       cluster onde o PVC não existe.
    3. **JSONL mtime** (fallback conservador): para dispatches pré-lease
       que não estão mais no ``/proc`` mas cujo JSONL foi appendado
       recentemente (sinal de vida cross-replica legado).

    O uso do lease como sinal primário resolve o bug histórico de
    triple-dispatch que ocorria quando o Service round-robin direcionava
    o request ``/resume-info`` para um pod diferente daquele onde claude
    girava — esse pod não via o processo no seu ``/proc`` local e respondia
    ``claude_alive=False``, levando o pipeline a disparar resume redundante.
    """
    # Sinal 1: lease no PVC compartilhado (cross-pod definitivo).
    lease_alive = _is_alive_via_lease(session_id)
    if lease_alive is not None:
        return lease_alive

    # Sinal 2: /proc local (para dispatches pré-lease ou fora do cluster).
    if _find_claude_pid(session_id) is not None:
        return True

    # Sinal 3: JSONL mtime (fallback legado cross-replica).
    return _is_session_jsonl_recently_active(session_id)


def _is_alive_via_lease(session_id: str) -> Optional[bool]:
    """Verifica liveness consultando o lease do workspace da sessão.

    Faz reverse-lookup de ``session_id`` → ``task_id`` → ``workdir`` via
    session metadata, depois lê ``workdir/.lease.json``.

    Returns:
        ``True``  — lease existe e heartbeat está dentro do TTL.
        ``False`` — lease existe mas expirou (task morta ou TTL passou).
        ``None``  — lease não existe (task pré-lease ou workdir perdido).
                    O caller deve checar sinais alternativos.
    """
    if not session_id:
        return None
    # Reverse-lookup: varre ~/.claude/tasks/ buscando session_id.
    task_id = _find_task_id_for_session(session_id)
    if not task_id:
        return None
    meta = _load_session_meta(task_id)
    if not meta:
        return None
    workdir_str = meta.get("workdir")
    if not workdir_str:
        return None
    lease_path = Path(workdir_str) / ".lease.json"
    if not lease_path.exists():
        return None
    try:
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        age = time.time() - float(lease.get("heartbeat_at", 0))
        return age < _LEASE_TTL_S
    except (OSError, json.JSONDecodeError, ValueError):
        # Lease corrompido → trata como morto, sinaliza para o caller
        # cair nos sinais alternativos.
        return None


def _find_task_id_for_session(session_id: str) -> Optional[str]:
    """Localiza o task_id pelo session_id fazendo scan do diretório de meta.

    O scan é O(n) em número de tasks no PVC; na prática cada pod tem poucas
    dezenas de tasks (a maioria é cleanup via /cleanup endpoint), então o custo
    é aceitável. Cache local não é implementado (risco de stale data).
    """
    base = _session_meta_dir()
    if not base.is_dir():
        return None
    try:
        children = list(base.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir() or not _TASK_ID_RE.fullmatch(child.name):
            continue
        meta = _load_session_meta(child.name)
        if meta and meta.get("session_id") == session_id:
            return child.name
    return None


def _find_claude_pid(session_id: str) -> Optional[int]:
    """Return the PID of the running ``claude`` for ``session_id``, or None.

    Same scan as :func:`_is_claude_process_alive`; broken out so the kill
    endpoint (issue #347) can target the discovered PID without needing
    the session metadata to remember it (avoids a race between persisting
    the PID and the actual fork).
    """
    if not session_id:
        return None
    proc_root = Path(_PROC_ROOT)
    if not proc_root.is_dir():
        return None
    target = session_id.encode("utf-8")
    try:
        for proc_dir in proc_root.iterdir():
            if not proc_dir.name.isdigit():
                continue
            cmdline_path = proc_dir / "cmdline"
            try:
                cmdline = cmdline_path.read_bytes().replace(b"\0", b" ")
            except OSError:
                continue
            if target in cmdline:
                try:
                    return int(proc_dir.name)
                except ValueError:
                    return None
    except OSError:
        return None
    return None


# --- Issue #347 follow-up: smart review resume helpers ----------------------
#
# Quando o pipeline resume com -r, ANTES do spawn:
#   1. Token budget check (sessão JSONL não pode crescer infinitamente)
#   2. git fast-forward no workdir (puxa commits novos do operador)
#
# Ambos best-effort: erros viram logger.warning, NÃO derrubam o dispatch.


def _resolve_jsonl_path(session_id: str, workspace: Path) -> Optional[Path]:
    """Localiza o JSONL da sessão claude no formato:

        ~/.claude/projects/-home-claude-work-<task_id>/<session_id>.jsonl

    onde ``<task_id>`` é derivado do nome do workspace. Retorna None se
    arquivo ausente.
    """
    if not session_id or not workspace:
        return None
    workspace_hash = "-".join(str(workspace).strip("/").split("/"))
    home = Path(os.environ.get("HOME", "/home/claude"))
    candidate = home / ".claude" / "projects" / f"-{workspace_hash}" / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    # Fallback: lista o dir e procura pelo session_id (algumas versões
    # do claude CLI normalizam hash de forma diferente).
    projects_dir = home / ".claude" / "projects"
    if projects_dir.is_dir():
        for sub in projects_dir.iterdir():
            if not sub.is_dir():
                continue
            f = sub / f"{session_id}.jsonl"
            if f.exists():
                return f
    return None


def _estimate_session_tokens(session_id: str, workspace: Path) -> int:
    """Soma usage tokens do JSONL da sessão claude (input + output + cache).

    Retorna 0 quando JSONL ausente / unreadable — fallback conservador
    (não bloqueia resume por incapacidade de medir).
    """
    jsonl_path = _resolve_jsonl_path(session_id, workspace)
    if jsonl_path is None:
        return 0
    total = 0
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = d.get("message") if isinstance(d, dict) else None
                usage = (msg or {}).get("usage") if isinstance(msg, dict) else None
                if not isinstance(usage, dict):
                    usage = d.get("usage") if isinstance(d, dict) else None
                if not isinstance(usage, dict):
                    continue
                for k in ("input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens"):
                    v = usage.get(k)
                    if isinstance(v, (int, float)):
                        total += int(v)
    except OSError as exc:
        logger.warning("session token count failed for %s: %s", session_id, exc)
        return 0
    return total


async def _git_fast_forward_workdir(workspace: Path, branch: Optional[str]) -> None:
    """Best-effort git fetch + reset --hard origin/<branch> dentro do
    workdir reaproveitado. Permite ao claude (em resume) ver commits
    novos pushados pelo operador entre revisions.

    Procura ``<workspace>/repo/.git`` (estrutura padrão criada pelo
    primeiro dispatch via ``gh repo clone repo``). Se não existir,
    no-op (claude pode fazer pull no próprio shell).

    Erros viram ``logger.warning`` e função retorna — NUNCA levanta.
    """
    if not branch or not workspace:
        return
    repo_dir = workspace / "repo"
    if not (repo_dir / ".git").exists():
        logger.debug("git ff: no .git in %s/repo — skipping", workspace)
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir), "fetch", "--quiet", "origin", branch,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("git fetch timeout in %s", repo_dir)
            return
        if proc.returncode != 0:
            logger.warning(
                "git fetch %s failed (rc=%d): %s", branch, proc.returncode,
                (err or b"").decode("utf-8", "replace")[:200],
            )
            return
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo_dir), "reset", "--hard", f"origin/{branch}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("git reset timeout in %s", repo_dir)
            return
        if proc.returncode == 0:
            logger.info("git ff %s/repo to origin/%s OK", workspace.name, branch)
        else:
            logger.warning(
                "git reset --hard origin/%s failed (rc=%d): %s", branch,
                proc.returncode, (err or b"").decode("utf-8", "replace")[:200],
            )
    except (FileNotFoundError, OSError) as exc:
        logger.warning("git ff failed in %s: %s", repo_dir, exc)


def _parse_claude_json_output(stdout: str) -> dict:
    """Extrai campos estruturados do ``--output-format json`` do claude CLI.

    Esperado: stdout é UM JSON object por dispatch (final result). Em caso
    de claude que CRASHOU antes de imprimir o JSON final (ex: kill -9),
    stdout pode estar truncado/vazio — retornamos dict default seguro.

    Returns:
        dict com chaves: ``is_error`` (bool), ``result`` (str),
        ``session_id`` (str), ``total_cost_usd`` (float),
        ``duration_ms`` (int), ``num_turns`` (int). Todas opcionais
        com defaults conservadores (is_error=True quando JSON ausente).
    """
    if not stdout or not stdout.strip():
        return {"is_error": True, "result": "", "session_id": "",
                "total_cost_usd": 0.0, "duration_ms": 0, "num_turns": 0}
    # Tentar o stdout inteiro primeiro (caso comum).
    try:
        data = json.loads(stdout.strip())
    except json.JSONDecodeError:
        # Fallback: pegar a ÚLTIMA linha que é JSON válido (caso o stdout
        # tenha logs antes do JSON final).
        data = None
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if data is None:
            return {"is_error": True, "result": "", "session_id": "",
                    "total_cost_usd": 0.0, "duration_ms": 0, "num_turns": 0}
    return {
        "is_error": bool(data.get("is_error", False)),
        "result": str(data.get("result", "") or ""),
        "session_id": str(data.get("session_id", "") or ""),
        "total_cost_usd": float(data.get("total_cost_usd", 0) or 0),
        "duration_ms": int(data.get("duration_ms", 0) or 0),
        "num_turns": int(data.get("num_turns", 0) or 0),
    }


# --------------------------------------------------------------------------- #
# Pod-status helpers (issue #395)
# --------------------------------------------------------------------------- #


def _find_active_lease(root: Path) -> Optional[dict]:
    """Return the most recent alive lease found under *root*/<task_id>/.lease.json.

    Security: exposes only task_id, heartbeat_at, pid — never the full
    lease payload (no pod name, no prompts, nothing injectable).
    """
    now = time.time()
    best: Optional[dict] = None
    best_hb: float = 0.0
    try:
        children = list(root.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not _TASK_ID_RE.fullmatch(child.name):
            continue
        lease_path = child / ".lease.json"
        if not lease_path.exists():
            continue
        try:
            data = json.loads(lease_path.read_text(encoding="utf-8"))
            hb = float(data.get("heartbeat_at", 0))
            if (now - hb) < _LEASE_TTL_S and hb > best_hb:
                best_hb = hb
                best = {
                    "task_id": child.name,
                    "heartbeat_at": hb,
                    "pid": data.get("pid"),
                }
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return best


# --------------------------------------------------------------------------- #
# PVC workspace auto-cleanup (Decisão #46)
# --------------------------------------------------------------------------- #
#
# O PVC ``/home/claude/work`` acumula um diretório por task_id. Sem
# limpeza, ele inchou para 122 workdirs / 1.9GB em produção. O cleanup
# legacy só roda no shutdown e nunca é executado em SIGKILL/OOM.
#
# Política:
#   * Startup hook: varre uma vez ao iniciar o servidor.
#   * Periodic task: a cada hora (configurável via env).
#   * Threshold trigger: quando o uso total passa de 1 GB, aplica modo
#     agressivo (10 min de TTL em vez de 30 min).
#
# Critério de remoção: o workspace é considerado abandonado quando
# ``.lease.json`` está ausente OU o ``heartbeat_at`` está mais velho que
# ``stale_threshold_s``. Workdirs com lease vivo nunca são tocados.

#: TTL padrão (30 min) — workdir sem heartbeat há mais que isso é stale.
_WORKSPACE_STALE_TTL_S: int = int(
    os.environ.get("DEILE_CLAUDE_WORKER_WORKSPACE_STALE_TTL_S", "1800"),
)

#: TTL agressivo (10 min) — aplicado quando o uso total estoura o cap.
_WORKSPACE_AGGRESSIVE_TTL_S: int = int(
    os.environ.get("DEILE_CLAUDE_WORKER_WORKSPACE_AGGRESSIVE_TTL_S", "600"),
)

#: Cap de uso do PVC em bytes (default 1 GB). Acima deste valor, o cleanup
#: usa o TTL agressivo. ``0`` desativa o trigger por tamanho.
_WORKSPACE_AGGRESSIVE_BYTES: int = int(
    os.environ.get("DEILE_CLAUDE_WORKER_WORKSPACE_CAP_BYTES", str(1024 * 1024 * 1024)),
)

#: Intervalo entre execuções periódicas (default 1 h).
_WORKSPACE_CLEANUP_INTERVAL_S: float = float(
    os.environ.get("DEILE_CLAUDE_WORKER_WORKSPACE_CLEANUP_INTERVAL_S", "3600"),
)


def _workspace_total_bytes(root: Path) -> int:
    """Estimativa rápida do tamanho total ocupado pela árvore de workdirs.

    Usa ``os.walk`` + ``st_size`` em vez de ``du -sb`` para não depender de
    um binário externo. Erros de I/O por entrada individual são ignorados
    (best-effort — o cleanup nunca deve abortar por um arquivo inacessível).
    """
    total = 0
    try:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                try:
                    total += os.stat(os.path.join(dirpath, f)).st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _workspace_is_stale(workspace: Path, *, threshold_s: int, now: float) -> bool:
    """True se ``workspace`` é candidato a remoção.

    Critério (em ordem de precedência):
      * ``.lease.json`` presente e ``heartbeat_at`` mais velho que
        ``threshold_s`` → stale (pod morreu sem cleanup).
      * ``.lease.json`` ausente: fallback no ``st_mtime`` do diretório
        (não removemos workdirs recém-criados que ainda não tiveram o
        primeiro heartbeat — evita race no startup do worker).
      * Erro de I/O → não remove (conservador).
    """
    lease = workspace / ".lease.json"
    try:
        st = lease.stat()
    except FileNotFoundError:
        # Sem lease: usa o mtime do próprio workdir como aproximação de
        # atividade — workdir recém-criado por um dispatch que ainda não
        # iniciou o heartbeat NÃO deve ser removido.
        try:
            ws_st = workspace.stat()
        except OSError:
            return False
        return (now - ws_st.st_mtime) >= threshold_s
    except OSError:
        # Permissão negada / erro de I/O — não removemos por segurança.
        return False
    # ``heartbeat_at`` é a fonte de verdade; se o arquivo está fresco
    # (st_mtime recente) mas o JSON aponta um heartbeat antigo, vale o JSON.
    try:
        data = json.loads(lease.read_text(encoding="utf-8"))
        hb = float(data.get("heartbeat_at", st.st_mtime))
    except (OSError, json.JSONDecodeError, ValueError):
        return True
    return (now - hb) >= threshold_s


def _remove_workspace_tree(workspace: Path) -> int:
    """Remove ``workspace`` recursivamente. Retorna bytes liberados (best-effort)."""
    import shutil as _sh  # local import (top-level só importa quando precisa)
    bytes_freed = 0
    try:
        for dirpath, _dirs, files in os.walk(workspace):
            for f in files:
                try:
                    bytes_freed += os.stat(os.path.join(dirpath, f)).st_size
                except OSError:
                    continue
    except OSError:
        pass
    try:
        _sh.rmtree(workspace, ignore_errors=True)
    except OSError as exc:  # pragma: no cover — rmtree(ignore_errors) já tolera
        logger.warning("rmtree falhou para %s: %s", workspace, exc)
    return bytes_freed


def _cleanup_stale_workspaces(
    root: Path, *, threshold_s: Optional[int] = None,
) -> dict:
    """Varre ``root`` e remove workdirs stale.

    Args:
        root: raiz do PVC (tipicamente ``/home/claude/work``).
        threshold_s: TTL em segundos. ``None`` resolve dinamicamente entre
            o TTL padrão e o agressivo via :data:`_WORKSPACE_AGGRESSIVE_BYTES`.

    Returns:
        dict com contadores ``inspected``, ``removed``, ``bytes_freed`` e
        ``threshold_s`` efetivamente aplicado (útil para audit).
    """
    summary = {
        "inspected": 0, "removed": 0, "bytes_freed": 0,
        "threshold_s": threshold_s or _WORKSPACE_STALE_TTL_S,
    }
    try:
        children = list(root.iterdir())
    except OSError as exc:
        logger.warning("workspace cleanup: cannot list root %s: %s", root, exc)
        return summary

    if threshold_s is None:
        # Modo automático: aplica TTL agressivo se o uso passou do cap.
        used = _workspace_total_bytes(root)
        if _WORKSPACE_AGGRESSIVE_BYTES > 0 and used > _WORKSPACE_AGGRESSIVE_BYTES:
            threshold_s = _WORKSPACE_AGGRESSIVE_TTL_S
            logger.warning(
                "workspace cleanup: uso=%d bytes > cap=%d — aplicando TTL "
                "agressivo (%ds)", used, _WORKSPACE_AGGRESSIVE_BYTES, threshold_s,
            )
        else:
            threshold_s = _WORKSPACE_STALE_TTL_S
        summary["threshold_s"] = threshold_s

    now = time.time()
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        # Só remove diretórios que parecem task_id (path-traversal containment).
        if not _TASK_ID_RE.fullmatch(child.name):
            continue
        summary["inspected"] += 1
        if not _workspace_is_stale(child, threshold_s=threshold_s, now=now):
            continue
        bytes_freed = _remove_workspace_tree(child)
        summary["removed"] += 1
        summary["bytes_freed"] += bytes_freed
        logger.info(
            "workspace cleanup removed task_id=%s bytes=%d threshold_s=%d",
            child.name, bytes_freed, threshold_s,
        )
    if summary["removed"]:
        logger.info(
            "workspace cleanup summary: inspected=%d removed=%d freed=%d bytes "
            "(threshold=%ds)",
            summary["inspected"], summary["removed"], summary["bytes_freed"],
            threshold_s,
        )
    return summary


async def _workspace_cleanup_loop(root: Path) -> None:
    """Task asyncio que roda :func:`_cleanup_stale_workspaces` periodicamente.

    Tolerante a erros: qualquer exceção é loggada e o loop continua.
    Cancelamento (shutdown) propaga via ``asyncio.CancelledError``.
    """
    while True:
        try:
            await asyncio.sleep(_WORKSPACE_CLEANUP_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        try:
            await asyncio.to_thread(_cleanup_stale_workspaces, root)
        except Exception as exc:  # noqa: BLE001 — loop nunca morre
            logger.warning("workspace cleanup loop: %s", exc)


def _count_claude_processes() -> int:
    """Count running claude processes via psutil (preferred) or pgrep fallback."""
    try:
        import psutil  # optional dep; absent outside cluster
        count = 0
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = proc.info.get("name") or ""
                cmdline = proc.info.get("cmdline") or []
                if name.startswith("claude") or (
                    cmdline and str(cmdline[0]).endswith("/claude")
                ):
                    count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return count
    except ImportError:
        pass
    # Fallback via pgrep (POSIX).
    try:
        result = subprocess.run(
            ["pgrep", "-fc", "^claude"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode in (0, 1):  # 1 = no processes found
            return int(result.stdout.strip() or "0")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return 0


# --------------------------------------------------------------------------- #
# In-pod OAuth broker (issue #335)
#
# Alternative to _oauth_server.py: exposes /v1/auth/start and /v1/auth/status
# directly in the existing claude_worker_server process (port 8767). Operates
# via the same kubectl port-forward tunnel used for regular dispatch.
#
# Security: /v1/auth/* are unauthenticated intentionally — they are called
# before any credential exists. The security boundary is kubectl port-forward.
# --------------------------------------------------------------------------- #


class _OAuthBrokerState:
    """Estado de um fluxo OAuth in-pod em andamento (singleton por processo)."""

    def __init__(self) -> None:
        self.status: str = "idle"
        self.oauth_url: Optional[str] = None
        self.callback_port: Optional[int] = None
        self.email: Optional[str] = None
        self.error: Optional[str] = None
        self.started_at: float = 0.0
        self._lock: threading.Lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.status = "idle"
            self.oauth_url = None
            self.callback_port = None
            self.email = None
            self.error = None
            self.started_at = 0.0


_oauth_broker = _OAuthBrokerState()

_CLAUDE_OAUTH_URL_RE = re.compile(r"https://(?:claude\.ai|anthropic\.com)/[^\s'\">]+")


def _run_in_pod_oauth(state: _OAuthBrokerState) -> None:
    """Background thread: executa ``claude auth login`` capturando URL OAuth."""
    creds_path = _creds_path()
    with state._lock:
        state.status = "pending"
        state.started_at = time.time()

    env = {**os.environ, "BROWSER": "", "DISPLAY": ""}
    try:
        proc = subprocess.Popen(
            ["claude", "auth", "login"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except FileNotFoundError:
        with state._lock:
            state.status = "error"
            state.error = "claude binary not found in PATH"
        return

    captured_url: Optional[str] = None
    for line in (proc.stdout or []):
        line = line.rstrip()
        logger.info("[in-pod-oauth] %s", line)
        if not captured_url:
            m = _CLAUDE_OAUTH_URL_RE.search(line)
            if m:
                captured_url = m.group()
                callback_port: Optional[int] = None
                try:
                    _qs = urllib.parse.parse_qs(urllib.parse.urlparse(captured_url).query)
                    _redirect_list = _qs.get("redirect_uri", [])
                    if _redirect_list:
                        _decoded = urllib.parse.unquote(_redirect_list[0])
                        _port_m = re.search(r"(?:localhost|127\.0\.0\.1):(\d+)", _decoded)
                        if _port_m:
                            callback_port = int(_port_m.group(1))
                except (ValueError, AttributeError):
                    pass
                with state._lock:
                    state.oauth_url = captured_url
                    state.callback_port = callback_port

    proc.wait()

    if proc.returncode == 0:
        email: Optional[str] = None
        if creds_path.exists():
            try:
                creds_data = json.loads(creds_path.read_text(encoding="utf-8"))
                oauth_data = creds_data.get("claudeAiOauth") if isinstance(creds_data, dict) else None
                email = (creds_data or {}).get("email")
                if not email and isinstance(oauth_data, dict):
                    email = oauth_data.get("email")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[in-pod-oauth] failed to read credentials.json: %s", exc)
        _refresh_oauth_with_lock(creds_path)
        with state._lock:
            state.status = "complete"
            state.email = email
    else:
        with state._lock:
            if state.status == "pending":
                state.status = "error"
                state.error = f"claude auth login exited with code {proc.returncode}"


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


async def health_handler(request: web.Request) -> web.Response:
    """Readiness/liveness — verifica que o ``claude`` está acessível no ``PATH``.

    O ``readinessProbe`` do Kubernetes consome este endpoint: 200 mantém o Pod
    no Service (aceitando dispatches); 500 removes do Service. Como rodamos
    com uma única réplica em V1, o sinal serve principalmente ao operador
    (Pod ``NotReady`` aparece em ``kubectl get pods``).
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        dlog.log_health_probe(request.path, 500)
        return web.json_response(
            {"status": "error", "error": "claude binary not found in PATH"},
            status=500,
        )
    dlog.log_health_probe(request.path, 200)
    return web.json_response({"status": "ok", "claude_binary": claude_bin})


async def auth_start_handler(request: web.Request) -> web.Response:
    """``GET /v1/auth/start`` — inicia fluxo OAuth in-pod (issue #335).

    Lança ``claude auth login`` em background thread com ``BROWSER=''``,
    captura a URL OAuth do stdout e retorna para o operador abrir no browser
    via ``kubectl port-forward``. Alternativa ao ``_oauth_server.py`` standalone.

    **Unauthenticated**: chamado antes de qualquer credencial existir.
    Security boundary = kubectl port-forward (só o operador com acesso
    kubectl ao cluster consegue alcançar este endpoint).

    Passe ``?reset=1`` para cancelar e reiniciar um fluxo em andamento.
    """
    reset = request.query.get("reset", "").lower() in ("1", "true", "yes")

    with _oauth_broker._lock:
        if reset and _oauth_broker.status == "pending":
            _oauth_broker.status = "error"
            _oauth_broker.error = "cancelled by ?reset=1"
        if _oauth_broker.status == "pending":
            return web.json_response({
                "status": "pending",
                "oauth_url": _oauth_broker.oauth_url,
                "callback_port": _oauth_broker.callback_port,
                "error": None,
                "tip": "OAuth already in progress; poll /v1/auth/status",
            })
        if _oauth_broker.status == "complete":
            return web.json_response({
                "status": "complete",
                "oauth_url": _oauth_broker.oauth_url,
                "callback_port": _oauth_broker.callback_port,
                "email": _oauth_broker.email,
                "error": None,
            })

    _oauth_broker.reset()
    t = threading.Thread(
        target=_run_in_pod_oauth, args=(_oauth_broker,), daemon=True,
    )
    t.start()

    deadline = time.time() + 20.0
    while time.time() < deadline:
        with _oauth_broker._lock:
            if _oauth_broker.oauth_url or _oauth_broker.status in ("error", "complete"):
                break
        await asyncio.sleep(0.25)

    with _oauth_broker._lock:
        return web.json_response({
            "status": _oauth_broker.status,
            "oauth_url": _oauth_broker.oauth_url,
            "callback_port": _oauth_broker.callback_port,
            "error": _oauth_broker.error,
            "tip": (
                "Open oauth_url in your browser; callback goes through "
                "kubectl port-forward to this pod"
            ) if _oauth_broker.oauth_url else (
                "URL not yet captured — retry in a few seconds or check pod logs"
            ),
        })


async def auth_status_handler(request: web.Request) -> web.Response:
    """``GET /v1/auth/status`` — status do fluxo OAuth in-pod.

    **Unauthenticated** — mesma razão de ``/v1/auth/start``.
    Poll até ``status == "complete"`` ou ``"error"``.
    """
    with _oauth_broker._lock:
        return web.json_response({
            "status": _oauth_broker.status,
            "oauth_url": _oauth_broker.oauth_url,
            "callback_port": _oauth_broker.callback_port,
            "email": _oauth_broker.email,
            "error": _oauth_broker.error,
            "started_at": _oauth_broker.started_at,
        })


async def pod_status_handler(request: web.Request) -> web.Response:
    """``GET /v1/pod-status`` — introspection of this pod's own runtime state.

    Returns a snapshot of:
    - ``lease``:           active lease (task_id, heartbeat_at, pid) or null when idle.
    - ``disk``:            PVC usage via shutil.disk_usage (no subprocess).
    - ``claude_processes``: count of running claude processes.
    - ``anthropic_quota``: last captured rate-limit tokens-remaining + timestamp,
                           or null if never observed.
    - ``ts``:              unix timestamp of this response.

    Security: ``lease`` exposes ONLY task_id, heartbeat_at, and pid — no
    prompt content, no credentials, nothing that could serve as an injection
    vector.  Bearer auth required (same middleware as all other endpoints).
    """
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    disk_mount = os.environ.get("DEILE_CLAUDE_HOME", "/home/claude")

    lease_info = await asyncio.to_thread(_find_active_lease, root)

    try:
        du = await asyncio.to_thread(shutil.disk_usage, disk_mount)
        disk_info: dict = {
            "used_bytes": du.used,
            "total_bytes": du.total,
            "mount": disk_mount,
        }
    except OSError as exc:
        logger.warning("disk_usage(%s) failed: %s", disk_mount, exc)
        disk_info = {"used_bytes": 0, "total_bytes": 0, "mount": disk_mount}

    claude_procs = await asyncio.to_thread(_count_claude_processes)

    quota_snap = _get_quota_snapshot()
    quota_info: Optional[dict] = None
    if quota_snap is not None:
        quota_info = {
            "tokens_remaining": quota_snap.tokens_remaining,
            "captured_at": quota_snap.captured_at,
        }

    return web.json_response({
        "lease": lease_info,
        "disk": disk_info,
        "claude_processes": claude_procs,
        "anthropic_quota": quota_info,
        "ts": time.time(),
    })


async def dispatch_handler(request: web.Request) -> web.Response:
    """``POST /v1/dispatch`` — executa ``claude -p`` em worktree isolado.

    Modos de execução:

    * **Fresh dispatch** (default): cria task_id novo + workspace + session-id
      UUID4. claude spawnado com ``--session-id <uuid>``. Metadata persistida
      em ``~/.claude/tasks/<task_id>/session.json`` antes E depois do spawn.
    * **Resume dispatch**: payload contém ``prev_task_id`` + ``resume_session_id``.
      Lê metadata do prev_task_id, reutiliza o workdir original (claude
      precisa do mesmo workspace pra resolver o JSONL da sessão), spawna com
      ``-r <session_id>`` em vez de ``--session-id``. Mesmo task_id é reutilizado
      e metadata é UPDATEada (attempt += 1, last_*).

    Sempre usa ``--output-format json``. O resultado JSON é parseado para
    extrair ``is_error``, ``result``, ``total_cost_usd`` — mais confiável
    que regex em stdout livre e detecta auth-expired estruturalmente
    (``is_error: true`` + ``result: 'Not logged in ...'``).

    Truncagem de tails: a resposta JSON limita ``stdout`` a 50 KiB e
    ``stderr`` a 10 KiB para não inflar o body — os logs completos ficam no
    PVC e podem ser inspecionados via ``/v1/progress`` ou ``kubectl exec``.
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": "invalid JSON"}, status=400,
        )

    brief = payload.get("brief")
    if not brief or not isinstance(brief, str):
        return web.json_response(
            {"ok": False, "error": "missing or invalid 'brief'"}, status=400,
        )

    stage = payload.get("stage", "implement")
    branch = payload.get("branch")
    model_slug = payload.get("preferred_model")
    resume_session_id = payload.get("resume_session_id")
    prev_task_id = payload.get("prev_task_id")
    # Reasoning effort por etapa → ``claude --effort``. Traduzido para um valor
    # que o CLI aceita (ultracode→xhigh, auto→omitir); inválido vira None (fail-open).
    # O painel oferece o vocabulário Claude Code completo; a tradução acontece aqui.
    _raw_reasoning = payload.get("preferred_reasoning")
    reasoning_effort = _coerce_claude_effort(_raw_reasoning)
    # Ultracode = xhigh (já no --effort) + keyword "workflow" no prompt para
    # opt-in no Workflow tool. Capturado do raw ANTES do coercion (que colapsa
    # ultracode→xhigh e perderia a distinção).
    is_ultracode = _is_ultracode(_raw_reasoning)
    # Pipeline-context fields (issue #396) — forwarded by the pipeline so
    # the PodWatchView panel can surface "what is this claude-worker doing"
    # in the WORK/LAST_COMPLETED header. Same wire contract as worker_server.
    _channel_id = str(payload.get("channel_id") or "").strip()
    _issue_number_raw = payload.get("issue_number")
    _issue_number: Optional[int]
    try:
        _issue_number = int(_issue_number_raw) if _issue_number_raw is not None else None
        if _issue_number is not None and _issue_number < 1:
            _issue_number = None
    except (TypeError, ValueError):
        _issue_number = None
    # Per-stage timeout override (issue #391). When set, overrides
    # DEILE_CLAUDE_WORKER_TASK_TIMEOUT_S for this dispatch only.
    dispatch_timeout_s: Optional[int] = None
    _raw_timeout = payload.get("timeout_s")
    if _raw_timeout is not None:
        try:
            _v = int(_raw_timeout)
            if _v > 0:
                dispatch_timeout_s = _v
        except (TypeError, ValueError):
            pass

    # claude-worker SÓ aceita anthropic:* — outros providers viraram 400.
    claude_model: Optional[str] = None
    if model_slug:
        match = _ANTHROPIC_SLUG_RE.match(model_slug)
        if not match:
            return web.json_response({
                "ok": False,
                "error": (
                    f"claude-worker requires 'anthropic:*' model, "
                    f"got {model_slug!r}"
                ),
            }, status=400)
        claude_model = match.group(1)

    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))

    # Resume path: reaproveita workdir + session-id existentes.
    is_resume = bool(resume_session_id and prev_task_id)
    if is_resume:
        # Validação do prev_task_id (path traversal + format).
        if not _TASK_ID_RE.fullmatch(prev_task_id or ""):
            return web.json_response({
                "ok": False,
                "error": f"invalid prev_task_id format {prev_task_id!r}",
            }, status=400)
        prev_meta = _load_session_meta(prev_task_id)
        if prev_meta is None:
            return web.json_response({
                "ok": False,
                "error_code": "RESUME_META_MISSING",
                "error": (
                    f"prev_task_id={prev_task_id!r} não tem session metadata "
                    f"(provavelmente pod foi recriado e PVC perdeu o arquivo). "
                    f"Pipeline deve fallback pra dispatch fresh."
                ),
            }, status=404)
        if prev_meta.get("session_id") != resume_session_id:
            return web.json_response({
                "ok": False,
                "error_code": "RESUME_SESSION_MISMATCH",
                "error": (
                    f"resume_session_id no payload não bate com session_id "
                    f"persistido no prev_task_id (corrupção do mini-ledger?)"
                ),
            }, status=409)
        task_id = prev_task_id
        session_id = resume_session_id
        workspace = Path(prev_meta.get("workdir") or (root / task_id))
        if not workspace.is_dir():
            return web.json_response({
                "ok": False,
                "error_code": "RESUME_WORKDIR_LOST",
                "error": (
                    f"workdir {workspace!s} sumiu (pod foi recriado em outro "
                    f"node, ou cleanup manual). Pipeline deve fallback pra "
                    f"dispatch fresh."
                ),
            }, status=410)
        attempt = int(prev_meta.get("attempt", 1)) + 1
        logger.info(
            "resume dispatch task_id=%s session=%s attempt=%d workdir=%s",
            task_id, session_id, attempt, workspace,
        )

        # Defense-in-depth contra triple-dispatch (corrigido 2026-05-27).
        # Se o claude da sessão anterior AINDA está vivo (no /proc local
        # OU sinalizado pelo mtime do JSONL na PVC compartilhada — qualquer
        # réplica), recusa o novo dispatch com 409. O caller (implementer.py)
        # converte isso em ``WorkOutcome(ok=False, error="DISPATCH_SKIPPED_...")``,
        # mantém a issue em ``~workflow:em_implementacao``, e o próximo tick
        # do monitor re-tenta — sem multiplicar Opus na mesma issue.
        if _is_claude_process_alive(session_id):
            logger.warning(
                "resume dispatch BLOCKED — claude session=%s ainda vivo "
                "(pid local ou JSONL recém-modificado em outra réplica). "
                "task_id=%s",
                session_id, task_id,
            )
            return web.json_response({
                "ok": False,
                "error_code": "CONCURRENT_DISPATCH_BLOCKED",
                "error": (
                    f"claude session_id={session_id!r} ainda em execução "
                    f"(detected via /proc local ou JSONL mtime); pipeline "
                    f"deve aguardar próximo tick"
                ),
                "task_id": task_id,
                "session_id": session_id,
            }, status=409)

        # Decisão #46 — Resume sob demanda: quando o resume é tentado e a
        # sessão JSONL acumulada passou de 100K tokens, ABANDONAR o resume
        # e PROMOVER fresh dispatch automaticamente. Antes rejeitávamos com
        # 413 (RESUME_BUDGET_EXCEEDED), o que parava trabalho — agora apenas
        # registramos a métrica e seguimos como fresh dispatch (novo task_id,
        # novo session_id, workdir fresco). O brief unified já lê
        # ``.deile-progress.md`` no PASSO 0, então o agente recupera o
        # contexto natural sem dependência do JSONL gigante.
        budget_limit = int(os.environ.get(
            "DEILE_CLAUDE_RESUME_TOKEN_BUDGET", "100000",
        ))
        if budget_limit > 0:
            jsonl_tokens = _estimate_session_tokens(session_id, workspace)
            if jsonl_tokens > budget_limit:
                logger.warning(
                    "resume %s session JSONL acumulou %d tokens "
                    "(>threshold=%d). Promovendo automaticamente para fresh "
                    "dispatch (brief unified lê .deile-progress.md no PASSO 0).",
                    session_id, jsonl_tokens, budget_limit,
                )
                # Promove para fresh: novo task_id, novo session_id, novo workdir.
                # Mantém branch e demais campos do payload.
                is_resume = False
                task_id = secrets.token_hex(8)
                session_id = str(uuid.uuid4())
                workspace = root / task_id
                workspace.mkdir(parents=True, exist_ok=True)
                attempt = 1
                logger.info(
                    "fresh-after-budget task_id=%s session=%s stage=%s "
                    "model=%s branch=%s",
                    task_id, session_id, stage, claude_model, branch,
                )

        # Issue #347 follow-up — GIT PULL no workdir reaproveitado pra que
        # claude veja os commits novos do operador. Best-effort: se falhar
        # (sem .git, sem origin, conflito), log warning e continua —
        # claude pode dar pull no próprio (tem `gh`/`git` no shell).
        await _git_fast_forward_workdir(workspace, branch)
    else:
        # Fresh path: novo task_id + session-id + workspace.
        task_id = secrets.token_hex(8)
        session_id = str(uuid.uuid4())
        workspace = root / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        attempt = 1
        logger.info(
            "fresh dispatch task_id=%s session=%s stage=%s model=%s branch=%s",
            task_id, session_id, stage, claude_model, branch,
        )

    # Preamble:
    #   • fresh dispatch — preamble do stage + brief original
    #   • resume — usa o BRIEF VINDO DO PAYLOAD se o pipeline já mandou um
    #     nudge contextual rico (issue #347 follow-up: _wrap_review_brief_for_resume
    #     constrói nudge com delta + comentários + checklist). Fallback
    #     pra nudge mínimo legacy quando brief é o default genérico.
    if is_resume:
        # Heurística: brief que começa com "# RESUME" (do nudge novo do
        # pipeline) é usado direto; brief sem marker = legacy → nudge mínimo.
        if brief.startswith("# RESUME") or "RESUME DE REVIEW" in brief[:200]:
            full_prompt = brief
        else:
            full_prompt = (
                f"Sua execução anterior (task_id={task_id}, attempt={attempt-1}) "
                f"foi interrompida (timeout, kill, pod restart). Você está sendo "
                f"retomado com `-r {session_id}` — você vê TODA a conversa "
                f"anterior, incluindo as ações já completadas (tool calls, files "
                f"editados, comments postados).\n\n"
                f"REGRA: NÃO refaça trabalho já completado (não re-comente, não "
                f"re-edite arquivos que já foram salvos com sucesso). Identifique "
                f"o ponto exato onde parou e continue. Finalize com 'STATUS: "
                f"SUCCESS' ou 'STATUS: BLOCKED_<motivo>' depois de postar comment "
                f"final se for review.\n\n"
                f"Contexto do operador (se presente):\n\n{brief}"
            )
    else:
        preamble = _render_preamble(stage, branch, task_id)
        full_prompt = preamble + "\n\n---\n\n" + brief

    # Ultracode: prefixa o keyword "workflow" para o CLI opt-in no Workflow tool
    # (multi-agente). Funciona em fresh e resume; se Dynamic Workflows estiver
    # off na conta, o CLI ignora o keyword sem quebrar o dispatch.
    if is_ultracode:
        full_prompt = _ULTRACODE_PREAMBLE + full_prompt

    # Structured dispatch marker consumed by WorkerProvider in the panel
    # (issue #396, #435). Emitted via dispatch_logger so PodWatchView can
    # show WORK/LAST_COMPLETED for claude-worker pods as well.
    dlog.dispatch_received(
        task=task_id,
        channel=_channel_id or "",
        stage=stage,
        issue=_issue_number,
        branch=branch,
        model_requested=claude_model,
        effort=reasoning_effort or None,
    )

    # Mecanismo 2 — Lease: garante que NUNCA dois pods trabalhem no mesmo
    # workspace simultaneamente. Adquirido ANTES do spawn; liberado no finally.
    lease = await _acquire_lease(workspace)
    if lease is None:
        logger.warning(
            "dispatch RECUSADO — lease ativo em %s (outro pod está trabalhando "
            "nesta task). task_id=%s stage=%s",
            workspace, task_id, stage,
        )
        # AC #435 §2 — 409 lease conflict é um dos 5 caminhos terminais
        # do dispatch e precisa emitir ``dispatch.failed`` pra o painel
        # liberar ``current_task`` (senão fica preso quando duas réplicas
        # brigam pelo mesmo workspace).
        dlog.dispatch_failed(
            task=task_id,
            reason="lease_conflict",
            error_code="TASK_ALREADY_RUNNING",
        )
        return web.json_response({
            "ok": False,
            "error_code": "TASK_ALREADY_RUNNING",
            "error": (
                f"outra réplica do claude-worker já está executando "
                f"task_id={task_id}; pipeline deve retry no próximo tick"
            ),
            "task_id": task_id,
        }, status=409)

    stop_hb = asyncio.Event()
    hb_task = asyncio.create_task(
        _heartbeat_loop(workspace / ".lease.json", stop_hb),
        name=f"lease-hb-{task_id}",
    )

    # Mecanismo 1 — OAuth: reload do token antes do spawn, serializado pelo
    # flock — garante que todos os pods leiam o token mais recente sem race.
    _refresh_oauth_with_lock()

    # Persistir metadata ANTES do spawn (pro endpoint /resume-info poder
    # detectar dispatches in-flight). Atomic via _save_session_meta.
    meta_pre = {
        "task_id": task_id,
        "session_id": session_id,
        "workdir": str(workspace),
        "stage": stage,
        "branch": branch,
        "model": claude_model,
        "reasoning_effort": reasoning_effort,
        "ultracode": is_ultracode,
        "started_at": int(time.time()),
        "attempt": attempt,
        "prev_task_id": prev_task_id if is_resume else None,
        "last_is_error": None,  # populado pós-spawn
        "last_result_summary": "",
        "last_returncode": None,
        "last_completed_at": None,
        "last_duration_seconds": None,
        "last_total_cost_usd": 0.0,
    }
    _save_session_meta(task_id, meta_pre)

    claude_bin = shutil.which("claude") or "claude"
    cmd = [
        claude_bin, "-p",
        "--permission-mode", "bypassPermissions",
        "--output-format", "json",
    ]
    if is_resume:
        cmd.extend(["-r", session_id])
    else:
        cmd.extend(["--session-id", session_id])
    if claude_model:
        cmd.extend(["--model", claude_model])
    if reasoning_effort:
        # Já coagido a um valor aceito pelo CLI ([a-z] puro) por _coerce_claude_effort.
        cmd.extend(["--effort", reasoning_effort])
    cmd.append(full_prompt)

    timeout = dispatch_timeout_s if dispatch_timeout_s is not None else int(
        os.environ.get("DEILE_CLAUDE_WORKER_TASK_TIMEOUT_S", "7200")
    )

    # Persist the command+prompt BEFORE the spawn so the observability panel
    # (issue #347) can show what's being executed even while it's running.
    meta_pre["command"] = list(cmd)
    meta_pre["full_prompt"] = full_prompt
    _save_session_meta(task_id, meta_pre)

    async def _cleanup_lease() -> None:
        """Para o heartbeat e libera o lease. Idempotente."""
        stop_hb.set()
        try:
            await hb_task
        except Exception:
            pass
        await _release_lease(workspace / ".lease.json")

    try:
        result = await run_subprocess_with_progress(
            cmd, cwd=workspace, task_id=task_id, timeout=timeout,
        )
    except Exception as exc:
        logger.exception("dispatch failed task_id=%s", task_id)
        meta_pre["last_is_error"] = True
        meta_pre["last_result_summary"] = f"{type(exc).__name__}: {exc}"[:300]
        meta_pre["last_returncode"] = -1
        meta_pre["last_completed_at"] = int(time.time())
        _save_session_meta(task_id, meta_pre)
        await _cleanup_lease()
        return web.json_response({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "task_id": task_id,
            "session_id": session_id,
        }, status=500)

    # Parse do JSON output (--output-format json) — fonte estruturada de
    # verdade pra is_error, result, cost. Resolve Bug A do Opus de forma
    # estrutural (sem regex frágil no stdout livre).
    claude_result = _parse_claude_json_output(result.stdout)

    # Best-effort quota capture (issue #395): scan stderr for rate-limit
    # header values printed by claude CLI.  O(1) em memória — nunca bloqueia.
    _try_capture_quota_from_output(result.stdout, result.stderr)

    # Detecção de auth expirado: estrutural (is_error=true + result contém
    # signature de auth) E fallback regex no stdout/stderr crus (pra casos
    # onde JSON output não veio — ex: timeout antes do final).
    auth_expired_struct = (
        claude_result["is_error"]
        and any(sig in claude_result["result"].lower()
                for sig in _AUTH_EXPIRED_SIGNATURES)
    )
    auth_expired_legacy = _detect_auth_expired(result.stdout, result.stderr)
    auth_expired = auth_expired_struct or auth_expired_legacy
    error_code = "WORKER_AUTH_EXPIRED" if auth_expired else None

    # Considera "ok" se: rc=0 AND não auth_expired AND JSON output não diz
    # is_error=true. JSON output sendo a fonte estrutural (claude pode
    # imprimir rc=0 mesmo em falha de auth — vide investigação Opus).
    ok = (
        result.returncode == 0
        and not auth_expired
        and not claude_result["is_error"]
    )

    # Persistir metadata final.
    meta_pre["last_is_error"] = claude_result["is_error"] or not ok
    meta_pre["last_result_summary"] = claude_result["result"][:300]
    meta_pre["last_returncode"] = result.returncode
    meta_pre["last_completed_at"] = int(time.time())
    meta_pre["last_duration_seconds"] = result.duration_seconds
    meta_pre["last_total_cost_usd"] = claude_result["total_cost_usd"]
    _save_session_meta(task_id, meta_pre)

    response = {
        "ok": ok,
        "stdout": result.stdout[-50_000:],
        "stderr": result.stderr[-10_000:],
        "task_id": task_id,
        "session_id": session_id,
        "attempt": attempt,
        "duration_seconds": result.duration_seconds,
        "returncode": result.returncode,
        "total_cost_usd": claude_result["total_cost_usd"],
        "num_turns": claude_result["num_turns"],
    }
    if error_code:
        response["error_code"] = error_code
        response["error"] = (
            "claude CLI reportou token OAuth expirado/inválido. "
            "Rode `deploy.py k8s claude-renew` no host pra renovar."
        )
    elif not ok and claude_result["is_error"] and claude_result["result"]:
        # Falha não-auth reportada pelo claude — propaga o erro pra pipeline.
        response["error"] = claude_result["result"][:500]

    # Terminal marker for the panel — pairs with dispatch.received (#435).
    if ok:
        dlog.dispatch_completed(
            task=task_id,
            ok=True,
            turns=claude_result.get("num_turns"),
            cost_usd=claude_result.get("total_cost_usd"),
            duration_s=result.duration_seconds,
        )
    else:
        _err_code = error_code or ("AUTH_EXPIRED" if auth_expired else None)
        dlog.dispatch_failed(
            task=task_id,
            reason=claude_result.get("result", "")[:120] or "unknown",
            turns=claude_result.get("num_turns"),
            duration_s=result.duration_seconds,
            error_code=_err_code,
        )

    # Libera heartbeat + lease antes de responder (lease liberado apenas aqui
    # no caminho feliz; o caminho de exceção já liberou no handler acima).
    await _cleanup_lease()
    return web.json_response(response)


#: Sinais textuais (case-insensitive) de auth expirado/inválido produzidos
#: pelo ``claude`` CLI no stdout/stderr. Mantenha conservador: melhor false
#: negative (não detecta auth_expired, dispatch falha genérico) que false
#: positive (marca como auth quando é outro erro — operador confunde).
_AUTH_EXPIRED_SIGNATURES = (
    "not logged in",
    "invalid authentication credentials",
    "401 unauthorized",
    "401 invalid authentication",
    "please run /login",
    "please run `claude auth login`",
)


def _detect_auth_expired(stdout: str, stderr: str) -> bool:
    """True se o output indica claramente OAuth token expirado/inválido.

    Conservador: requer match de string específica do claude CLI, não
    apenas "401" genérico (pode ser HTTP erro de outra source). False
    se nenhum sinal — outros erros caem em ``ok=False`` genérico (com
    ``returncode != 0``) e o pipeline trata como falha normal.
    """
    combined = (stdout + "\n" + stderr).lower()
    return any(sig in combined for sig in _AUTH_EXPIRED_SIGNATURES)


async def progress_handler(request: web.Request) -> web.Response:
    """``GET /v1/progress/{task_id}`` — snapshot do task em execução ou completo.

    Lê os arquivos persistidos por :func:`run_subprocess_with_progress` no PVC
    (``DEILE_CLAUDE_WORKER_ROOT/.progress/<task_id>.<stream>.log``) e devolve
    tail (stdout 50 KiB, stderr 10 KiB). Usado pelo painel TUI / subagent
    orchestration para acompanhar mid-flight sem aguardar a resposta do
    ``/v1/dispatch``.

    Returns:
        - ``200`` com ``{task_id, stdout, stderr}`` se algum dos arquivos existe.
        - ``404`` se ``task_id`` tem formato válido mas nenhum dos arquivos
          de progress está presente (task ainda não rodou, foi GCed, etc.).
        - ``400`` se ``task_id`` não bate ``[0-9a-f]{16}`` — defende contra
          path traversal pela URL e contra IDs vazados de outros sistemas.

    Erros de I/O ao ler os arquivos viram ``logger.warning`` + string vazia
    (best-effort): o que o cliente vê é o que conseguimos ler.
    """
    task_id = request.match_info["task_id"]

    # Sanity: task_id deve ser hex 16-char (gerado por secrets.token_hex(8)).
    if not _TASK_ID_RE.fullmatch(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )

    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    progress_dir = root / ".progress"
    stdout_path = progress_dir / f"{task_id}.stdout.log"
    stderr_path = progress_dir / f"{task_id}.stderr.log"

    if not stdout_path.exists() and not stderr_path.exists():
        return web.json_response(
            {"error": f"task_id {task_id} not found"},
            status=404,
        )

    try:
        stdout = stdout_path.read_text() if stdout_path.exists() else ""
    except OSError as exc:
        logger.warning("failed to read %s: %s", stdout_path, exc)
        stdout = ""

    try:
        stderr = stderr_path.read_text() if stderr_path.exists() else ""
    except OSError as exc:
        logger.warning("failed to read %s: %s", stderr_path, exc)
        stderr = ""

    return web.json_response({
        "task_id": task_id,
        "stdout": stdout[-50_000:],
        "stderr": stderr[-10_000:],
    })


async def resume_info_handler(request: web.Request) -> web.Response:
    """``GET /v1/dispatches/{task_id}/resume-info`` — snapshot do session
    metadata pra decisão de resume vs fresh dispatch no pipeline.

    Returns:
        - ``200`` com ``{task_id, session_id, workdir, workdir_exists,
          stage, branch, started_at, last_completed_at, last_is_error,
          last_result_summary, attempt, claude_alive}``.
        - ``404`` se task_id válido mas sem session metadata (task nunca
          rodou no PVC atual — pode ter sido GCed ou pod foi recriado).
        - ``400`` se task_id não bate ``[0-9a-f]{16}`` (path traversal guard).

    ``claude_alive`` é heurística (``pgrep -f claude | grep <session_id>``):
    true = ainda há processo claude rodando com esse session-id na cmdline,
    false = processo morreu ou nunca existiu. Pipeline usa pra decidir entre
    "ainda rodando, não disturbar" vs "morto, pode resume". Best-effort:
    em erro de pgrep retorna false (assume morto — pipeline retry retoma).
    """
    task_id = request.match_info["task_id"]
    if not _TASK_ID_RE.fullmatch(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )
    meta = _load_session_meta(task_id)
    if meta is None:
        return web.json_response(
            {"error": f"task_id {task_id} not found in session metadata"},
            status=404,
        )
    workdir = Path(meta.get("workdir", "") or "")
    session_id = meta.get("session_id", "") or ""
    return web.json_response({
        "task_id": task_id,
        "session_id": session_id,
        "workdir": str(workdir),
        "workdir_exists": workdir.is_dir(),
        "stage": meta.get("stage"),
        "branch": meta.get("branch"),
        "model": meta.get("model"),
        "started_at": meta.get("started_at"),
        "last_completed_at": meta.get("last_completed_at"),
        "last_is_error": meta.get("last_is_error"),
        "last_result_summary": (meta.get("last_result_summary") or "")[:300],
        "last_returncode": meta.get("last_returncode"),
        "last_duration_seconds": meta.get("last_duration_seconds"),
        "last_total_cost_usd": meta.get("last_total_cost_usd"),
        "attempt": meta.get("attempt", 1),
        "prev_task_id": meta.get("prev_task_id"),
        "claude_alive": _is_claude_process_alive(session_id),
    })


# --------------------------------------------------------------------------- #
# Observability endpoints (issue #347)
#
# These five read-only endpoints plus the ``kill``/``cleanup`` mutating pair
# are consumed by the new TUI observability panel in
# ``deile/ui/panel/observability/``.  They live alongside ``/v1/dispatch``
# (operator-facing) but never block it — every handler is best-effort, never
# spawns a subprocess, and only reads from the PVC.
# --------------------------------------------------------------------------- #


#: Environment variable name patterns that SHOULD be redacted even when the
#: value itself doesn't match a known secret pattern (defense-in-depth).
_REDACTED_KEY_PATTERNS = (
    re.compile(r".*_API_KEY$", re.IGNORECASE),
    re.compile(r".*_TOKEN$", re.IGNORECASE),
    re.compile(r".*_SECRET$", re.IGNORECASE),
    re.compile(r".*PASSWORD.*", re.IGNORECASE),
    re.compile(r"^ANTHROPIC_AUTH_TOKEN$", re.IGNORECASE),
    re.compile(r"^DEILE_.*_AUTH_TOKEN$", re.IGNORECASE),
)


def _redact_env(env: dict) -> dict:
    """Return a copy of ``env`` with sensitive values redacted.

    Two-layer defense:

    1. **Key-based** — any env-var whose *name* matches
       :data:`_REDACTED_KEY_PATTERNS` (``*_API_KEY``, ``*_TOKEN``, etc.)
       is redacted unconditionally.
    2. **Value-based** — uses
       :class:`deile.security.secrets_scanner.SecretsScanner.redact_text`
       as the single source of truth for detecting secret patterns in the
       value itself (e.g. GitHub tokens, AWS keys, private keys).

    A key hit OR a value hit replaces the entry with ``***``.
    Non-string values are coerced to ``str``.
    """
    from deile.security.secrets_scanner import SecretsScanner

    scanner = SecretsScanner()
    out = {}
    for key, value in env.items():
        if not isinstance(key, str):
            continue
        str_value = value if isinstance(value, str) else str(value)
        # Layer 1: key-name match
        key_sensitive = any(pat.search(key) for pat in _REDACTED_KEY_PATTERNS)
        if key_sensitive:
            out[key] = "***"
            continue
        # Layer 2: value-content scan via SecretsScanner
        redacted_value, _matches = scanner.redact_text(str_value)
        out[key] = "***" if redacted_value != str_value else str_value
    return out


def _claude_jsonl_path(workdir: str, session_id: str) -> Optional[Path]:
    """Compute the ``~/.claude/projects/<workspace-hash>/<sid>.jsonl`` path.

    Claude derives ``<workspace-hash>`` by replacing ``/`` with ``-`` in the
    absolute workdir.  The result is anchored at ``$HOME/.claude/projects/``.
    Returns ``None`` if either input is empty.
    """
    if not workdir or not session_id:
        return None
    home = Path(os.environ.get("HOME", "/home/claude"))
    workspace_hash = "-" + workdir.lstrip("/").replace("/", "-")
    return home / ".claude" / "projects" / workspace_hash / f"{session_id}.jsonl"


def _summarize_session_meta(task_id: str, meta: dict) -> dict:
    """Project the on-disk ``session.json`` into the listing payload.

    The listing must stay narrow — the panel polls it at ~1 Hz and only
    needs enough state to render a row.  Full detail is fetched on demand
    via the per-task endpoints below.
    """
    session_id = meta.get("session_id") or ""
    return {
        "task_id": task_id,
        "session_id": session_id,
        "stage": meta.get("stage"),
        "branch": meta.get("branch"),
        "model": meta.get("model"),
        "attempt": meta.get("attempt", 1),
        "started_at": meta.get("started_at"),
        "last_completed_at": meta.get("last_completed_at"),
        "last_is_error": meta.get("last_is_error"),
        "last_returncode": meta.get("last_returncode"),
        "last_duration_seconds": meta.get("last_duration_seconds"),
        "last_total_cost_usd": meta.get("last_total_cost_usd"),
        "alive": _is_claude_process_alive(session_id),
        "workdir": meta.get("workdir"),
        "workdir_exists": bool(meta.get("workdir") and Path(meta["workdir"]).is_dir()),
    }


async def sessions_list_handler(request: web.Request) -> web.Response:
    """``GET /v1/sessions`` — list of all tasks the worker remembers.

    Walks ``~/.claude/tasks/<task_id>/session.json`` and returns one summary
    per valid task.  Orphan directories (missing ``session.json`` or whose
    name does not match the hex pattern) are silently skipped so a partial
    PVC does not bleed garbage into the panel.
    """
    base = _session_meta_dir()
    items: List[dict] = []
    if base.is_dir():
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            logger.warning("could not list %s: %s", base, exc)
            children = []
        for child in children:
            if not child.is_dir():
                continue
            if not _TASK_ID_RE.fullmatch(child.name):
                continue
            meta = _load_session_meta(child.name)
            if meta is None:
                continue
            items.append(_summarize_session_meta(child.name, meta))
    # Sort newest-first by started_at so the most recent task shows up first.
    items.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
    return web.json_response({"sessions": items})


async def sessions_command_handler(request: web.Request) -> web.Response:
    """``GET /v1/sessions/{task_id}/command`` — exact command line used.

    The payload exposes the full ``argv`` list and the verbatim prompt that
    was passed to ``claude -p``.  Sensitive environment variables are
    redacted via :func:`_redact_env` before they leave the pod.  Useful for
    the panel's ``[c] full command`` overlay.
    """
    task_id = request.match_info["task_id"]
    if not _TASK_ID_RE.fullmatch(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )
    meta = _load_session_meta(task_id)
    if meta is None:
        return web.json_response(
            {"error": f"task_id {task_id} not found"}, status=404,
        )
    return web.json_response({
        "task_id": task_id,
        "cmd": meta.get("command") or [],
        "full_prompt": meta.get("full_prompt") or "",
        "stage": meta.get("stage"),
        "branch": meta.get("branch"),
        "model": meta.get("model"),
        "subprocess_pid": meta.get("subprocess_pid"),
        "env_redacted": _redact_env(dict(os.environ)),
    })


async def sessions_chat_handler(request: web.Request) -> web.Response:
    """``GET /v1/sessions/{task_id}/chat?tail=N`` — parsed JSONL turns.

    Re-uses :class:`deile.ui.panel.observability.jsonl_parser.ClaudeJsonlParser`
    from inside the worker so the panel does not need PVC access — the
    structured turn list is returned over HTTP.  ``tail`` defaults to ``50``
    (capped at ``200`` to keep responses bounded).
    """
    task_id = request.match_info["task_id"]
    if not _TASK_ID_RE.fullmatch(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )
    meta = _load_session_meta(task_id)
    if meta is None:
        return web.json_response(
            {"error": f"task_id {task_id} not found"}, status=404,
        )

    try:
        tail = int(request.query.get("tail", "50"))
    except ValueError:
        tail = 50
    tail = max(1, min(tail, 200))

    workdir = meta.get("workdir") or ""
    session_id = meta.get("session_id") or ""
    jsonl_path = _claude_jsonl_path(workdir, session_id)
    if not jsonl_path or not jsonl_path.exists():
        return web.json_response({
            "task_id": task_id,
            "session_id": session_id,
            "jsonl_path": str(jsonl_path) if jsonl_path else None,
            "turns": [],
            "missing": True,
        })

    parser = _load_jsonl_parser()
    if parser is None:
        return web.json_response(
            {"error": "jsonl parser unavailable"}, status=500,
        )
    result = parser(jsonl_path).parse_all(max_turns=tail)
    return web.json_response({
        "task_id": task_id,
        "session_id": session_id,
        "jsonl_path": str(jsonl_path),
        "turns": [_turn_to_payload(t) for t in result.turns],
        "skipped_malformed_lines": result.skipped_malformed_lines,
    })


async def sessions_stdout_handler(request: web.Request) -> web.Response:
    """``GET /v1/sessions/{task_id}/stdout?tail_bytes=K`` — raw tail of logs.

    Alias-with-knobs over the progress file pair.  ``tail_bytes`` caps each
    of stdout/stderr (default 8 KiB each, max 50 KiB) — the panel keeps the
    chat view structured and reaches into this endpoint only when the
    operator presses ``[t]`` to inspect untruncated logs.
    """
    task_id = request.match_info["task_id"]
    if not _TASK_ID_RE.fullmatch(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )
    try:
        tail_bytes = int(request.query.get("tail_bytes", "8192"))
    except ValueError:
        tail_bytes = 8192
    tail_bytes = max(64, min(tail_bytes, 50_000))

    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    stdout_path = root / ".progress" / f"{task_id}.stdout.log"
    stderr_path = root / ".progress" / f"{task_id}.stderr.log"

    if not stdout_path.exists() and not stderr_path.exists():
        return web.json_response(
            {"error": f"task_id {task_id} not found"}, status=404,
        )

    def _tail(p: Path) -> str:
        try:
            data = p.read_text() if p.exists() else ""
        except OSError as exc:
            logger.warning("failed to read %s: %s", p, exc)
            return ""
        return data[-tail_bytes:]

    return web.json_response({
        "task_id": task_id,
        "stdout": _tail(stdout_path),
        "stderr": _tail(stderr_path),
        "tail_bytes": tail_bytes,
    })


async def sessions_kill_handler(request: web.Request) -> web.Response:
    """``POST /v1/sessions/{task_id}/kill`` — terminate the running subprocess.

    The body MUST contain ``{"confirm": "yes-task-<first 8 hex chars>"}`` —
    a deliberate confirmation token so the panel cannot kill a task by
    accident (e.g. operator hitting ``[k]`` on the wrong row).  Returns
    ``400`` without the token, ``404`` when the task is unknown, and ``409``
    when there is no live process to kill.
    """
    task_id = request.match_info["task_id"]
    if not _TASK_ID_RE.fullmatch(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    expected_confirm = f"yes-task-{task_id[:8]}"
    if not isinstance(body, dict) or body.get("confirm") != expected_confirm:
        return web.json_response({
            "error": "missing or invalid confirm token",
            "expected": expected_confirm,
        }, status=400)
    meta = _load_session_meta(task_id)
    if meta is None:
        return web.json_response(
            {"error": f"task_id {task_id} not found"}, status=404,
        )
    session_id = meta.get("session_id") or ""
    pid = _find_claude_pid(session_id)
    if pid is None:
        return web.json_response({
            "killed": False,
            "task_id": task_id,
            "reason": "no live claude subprocess",
        }, status=409)
    try:
        os.kill(pid, 9)
    except OSError as exc:
        logger.warning("kill(%s) failed: %s", pid, exc)
        return web.json_response({
            "killed": False,
            "task_id": task_id,
            "reason": str(exc),
        }, status=500)
    return web.json_response({"killed": True, "task_id": task_id, "pid": pid})


async def sessions_cleanup_handler(request: web.Request) -> web.Response:
    """``DELETE /v1/sessions/{task_id}/cleanup`` — drop workdir + jsonl + meta.

    The intent is reclaiming PVC space, NOT cancelling a task — refuses to
    proceed when the task is still alive (``409``).  Removal is best-effort
    and the response payload lists exactly what was removed; partial cleanup
    is still considered a success because the dominant cost is the workdir.
    """
    task_id = request.match_info["task_id"]
    if not _TASK_ID_RE.fullmatch(task_id):
        return web.json_response(
            {"error": "invalid task_id format (expected hex 16-char)"},
            status=400,
        )
    meta = _load_session_meta(task_id)
    if meta is None:
        return web.json_response(
            {"error": f"task_id {task_id} not found"}, status=404,
        )
    if _is_claude_process_alive(meta.get("session_id") or ""):
        return web.json_response({
            "error": "task is alive — kill first",
            "task_id": task_id,
        }, status=409)

    removed = {"workdir": False, "session": False, "jsonl": False, "progress": False}

    workdir = meta.get("workdir")
    if workdir:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
            removed["workdir"] = not Path(workdir).exists()
        except OSError as exc:
            logger.warning("rmtree(%s) failed: %s", workdir, exc)

    jsonl_path = _claude_jsonl_path(workdir or "", meta.get("session_id") or "")
    if jsonl_path and jsonl_path.exists():
        try:
            jsonl_path.unlink()
            removed["jsonl"] = True
        except OSError as exc:
            logger.warning("unlink(%s) failed: %s", jsonl_path, exc)

    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    for stream in ("stdout", "stderr"):
        p = root / ".progress" / f"{task_id}.{stream}.log"
        if p.exists():
            try:
                p.unlink()
                removed["progress"] = True
            except OSError as exc:
                logger.warning("unlink(%s) failed: %s", p, exc)

    session_dir = _session_meta_dir() / task_id
    if session_dir.exists():
        try:
            shutil.rmtree(session_dir, ignore_errors=True)
            removed["session"] = not session_dir.exists()
        except OSError as exc:
            logger.warning("rmtree(%s) failed: %s", session_dir, exc)

    return web.json_response({"task_id": task_id, "removed": removed})


# Lazy import of the JSONL parser — avoids hard-coupling the worker module
# to the panel package at import time (the worker runs in pods that may not
# need the panel sources, e.g. headless CI).
def _load_jsonl_parser():
    """Return :class:`ClaudeJsonlParser` or ``None`` if unavailable."""
    try:
        # ``infra/k8s/`` is NOT a Python package; the panel package lives in
        # ``deile/ui/panel/observability/``.  In the worker pod the whole
        # repo is laid out at ``/app`` and the ``deile`` package is on
        # ``sys.path`` (see ``wrapper.py``).  Outside the pod the import
        # also works as long as the repo root is on ``sys.path`` (the
        # default for pytest invocations).
        from deile.ui.panel.observability.jsonl_parser import ClaudeJsonlParser
        return ClaudeJsonlParser
    except Exception as exc:  # broad: this is best-effort soft-import
        logger.warning("could not import ClaudeJsonlParser: %s", exc)
        return None


def _turn_to_payload(turn) -> dict:
    """Serialize a parser Turn dataclass to a JSON-safe dict."""
    payload = {
        "index": turn.index,
        "ts": turn.ts,
        "role": getattr(turn, "role", None),
        "in_progress": getattr(turn, "in_progress", False),
        "type": turn.__class__.__name__,
    }
    for attr in ("content", "tool_name", "tool_input", "tool_use_id",
                 "is_error", "model", "stop_reason", "usage",
                 "type_label", "summary"):
        if hasattr(turn, attr):
            value = getattr(turn, attr)
            # Coerce content blocks to short str; tool_input is already dict.
            payload[attr] = value
    return payload


# --------------------------------------------------------------------------- #
# Housekeeping / garbage collection (issue #408)
#
# _cleanup_scan  — descobre workdirs/leases candidatos a remoção (idempotente,
#                  só lê disco).
# _do_cleanup    — executa a remoção com dry_run opcional; chama _cleanup_scan
#                  internamente.
# cleanup_handler / cleanup_execute_handler — HTTP endpoints de preview/execute.
# _startup_cleanup — wrapper chamado pelo main() no startup (best-effort).
#
# Critério conservador (nunca afeta workdirs com lease ativo):
#   1. Lease expirado (heartbeat > TTL) + PID inexistente → remove lease só.
#   2. Workdir sem last_modified mais velho que DEILE_CLAUDE_CLEANUP_RETENTION_DAYS
#      → remove workdir inteiro.
#   3. Workdir sem session JSONL (claude nunca rodou) → remove workdir inteiro.
# --------------------------------------------------------------------------- #

#: Env var que controla a retenção. Default conservador = 7 dias.
_CLEANUP_RETENTION_DAYS_ENV = "DEILE_CLAUDE_CLEANUP_RETENTION_DAYS"
_CLEANUP_RETENTION_DAYS_DEFAULT = 7


def _is_pid_alive(pid: int) -> bool:
    """True se o processo ``pid`` existe no /proc local."""
    try:
        return Path(f"/proc/{pid}").is_dir()
    except OSError:
        return False


def _session_jsonl_exists_for_workdir(workdir: Path) -> bool:
    """True se há pelo menos um JSONL de sessão claude no diretório de
    projetos correspondente a este workdir.

    O JSONL fica em ``~/.claude/projects/-home-claude-work-<task_id>/``.
    """
    task_id = workdir.name
    home = Path(os.environ.get("HOME", "/home/claude"))
    workspace_hash = "-home-claude-work-" + task_id
    project_dir = home / ".claude" / "projects" / workspace_hash
    if not project_dir.is_dir():
        return False
    try:
        return any(project_dir.glob("*.jsonl"))
    except OSError:
        return False


def _cleanup_scan(
    root: Path,
    retention_days: int = _CLEANUP_RETENTION_DAYS_DEFAULT,
) -> dict:
    """Varre PVC root e retorna candidatos a remoção sem deletar nada.

    Returns dict:
        dead_leases  — list[str] de paths de lease expirado + PID morto
        old_workdirs — list[str] de workdirs além da janela de retenção
        empty_workdirs — list[str] de workdirs sem session JSONL
        active_workdirs — list[str] de workdirs com lease ativo (skip)
        total_candidate_bytes — soma estimada de bytes dos candidatos
    """
    if not root.is_dir():
        return {
            "dead_leases": [], "old_workdirs": [],
            "empty_workdirs": [], "active_workdirs": [],
            "total_candidate_bytes": 0,
        }

    now = time.time()
    retention_s = retention_days * 86400
    dead_leases: list = []
    old_workdirs: list = []
    empty_workdirs: list = []
    active_workdirs: list = []
    candidate_bytes: int = 0

    try:
        children = [p for p in root.iterdir() if p.is_dir()
                    and _TASK_ID_RE.fullmatch(p.name)]
    except OSError:
        children = []

    for workdir in children:
        lease_path = workdir / ".lease.json"

        # Check 1: active lease → always skip.
        is_active = False
        if lease_path.exists():
            try:
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                age = now - float(lease.get("heartbeat_at", 0))
                if age < _LEASE_TTL_S:
                    is_active = True
            except (OSError, json.JSONDecodeError, ValueError):
                pass  # corrompto → não ativo

        if is_active:
            active_workdirs.append(str(workdir))
            continue

        # Check 2: dead lease (expired + PID dead) → candidate lease-only.
        if lease_path.exists():
            try:
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                pid = lease.get("pid")
                if pid and not _is_pid_alive(int(pid)):
                    dead_leases.append(str(lease_path))
                    candidate_bytes += lease_path.stat().st_size
            except (OSError, json.JSONDecodeError, ValueError):
                dead_leases.append(str(lease_path))

        # Check 3: workdir last modified > retention → remove entire dir.
        try:
            mtime = workdir.stat().st_mtime
            if (now - mtime) > retention_s:
                old_workdirs.append(str(workdir))
                candidate_bytes += _dir_size(workdir)
                continue
        except OSError:
            pass

        # Check 4: no session JSONL → remove entire dir.
        if not _session_jsonl_exists_for_workdir(workdir):
            empty_workdirs.append(str(workdir))
            candidate_bytes += _dir_size(workdir)

    return {
        "dead_leases": dead_leases,
        "old_workdirs": old_workdirs,
        "empty_workdirs": empty_workdirs,
        "active_workdirs": active_workdirs,
        "total_candidate_bytes": candidate_bytes,
    }


def _dir_size(p: Path) -> int:
    """Soma recursiva do tamanho dos arquivos em ``p``. Best-effort: erros → 0."""
    total = 0
    try:
        for child in p.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _do_cleanup(
    root: Path,
    retention_days: int = _CLEANUP_RETENTION_DAYS_DEFAULT,
    *,
    dry_run: bool = False,
) -> dict:
    """Executa a remoção de workdirs/leases candidatos.

    Returns dict com o scan + ``removed_leases``, ``removed_workdirs``,
    ``freed_bytes``.  Em ``dry_run=True`` retorna apenas o scan sem deletar.
    """
    scan = _cleanup_scan(root, retention_days)
    removed_leases: list = []
    removed_workdirs: list = []
    freed_bytes: int = 0

    if dry_run:
        return {**scan, "removed_leases": [], "removed_workdirs": [],
                "freed_bytes": 0, "dry_run": True}

    for lease_path_str in scan["dead_leases"]:
        try:
            p = Path(lease_path_str)
            size = p.stat().st_size if p.exists() else 0
            p.unlink(missing_ok=True)
            removed_leases.append(lease_path_str)
            freed_bytes += size
        except OSError as exc:
            logger.warning("cleanup: falha ao remover lease %s: %s",
                           lease_path_str, exc)

    for workdir_str in scan["old_workdirs"] + scan["empty_workdirs"]:
        # Dedup: um workdir pode aparecer em ambas as listas.
        if workdir_str in removed_workdirs:
            continue
        try:
            p = Path(workdir_str)
            size = _dir_size(p)
            shutil.rmtree(p, ignore_errors=True)
            if not p.exists():
                removed_workdirs.append(workdir_str)
                freed_bytes += size
        except OSError as exc:
            logger.warning("cleanup: falha ao remover workdir %s: %s",
                           workdir_str, exc)

    return {
        **scan,
        "removed_leases": removed_leases,
        "removed_workdirs": removed_workdirs,
        "freed_bytes": freed_bytes,
        "dry_run": False,
    }


def _startup_cleanup(root: Path) -> None:
    """Hook de startup: varre o PVC e remove lixo acumulado.

    Chamado em ``main()`` antes de aceitar conexões. Best-effort: erros
    viram logger.warning, nunca derrubam o servidor.
    """
    retention_days = int(
        os.environ.get(_CLEANUP_RETENTION_DAYS_ENV,
                       str(_CLEANUP_RETENTION_DAYS_DEFAULT))
    )
    try:
        result = _do_cleanup(root, retention_days)
        n_leases = len(result.get("removed_leases", []))
        n_dirs = len(result.get("removed_workdirs", []))
        freed = result.get("freed_bytes", 0)
        logger.info(
            "startup_cleanup: removidos %d leases + %d workdirs, "
            "%.1f MiB liberados (retention=%d dias)",
            n_leases, n_dirs, freed / 1_048_576, retention_days,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("startup_cleanup falhou (ignorando): %s", exc)


def _run_cleanup_main() -> int:
    """Entry point para o CronJob e modo ``--cleanup`` CLI.

    Lê env vars (HOME, DEILE_CLAUDE_WORKER_ROOT, DEILE_CLAUDE_CLEANUP_RETENTION_DAYS),
    executa o cleanup e imprime JSON do resultado para stdout.
    Retorna 0 em sucesso, 1 em erro.
    """
    _log_level = os.environ.get("DEILE_CLAUDE_WORKER_LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=_log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    retention_days = int(
        os.environ.get(_CLEANUP_RETENTION_DAYS_ENV,
                       str(_CLEANUP_RETENTION_DAYS_DEFAULT))
    )
    try:
        result = _do_cleanup(root, retention_days)
        print(json.dumps(result, indent=2))
        logger.info(
            "cleanup_main: %d leases + %d workdirs removidos, %.1f MiB liberados",
            len(result.get("removed_leases", [])),
            len(result.get("removed_workdirs", [])),
            result.get("freed_bytes", 0) / 1_048_576,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("cleanup_main erro: %s", exc, exc_info=True)
        return 1


async def cleanup_preview_handler(request: web.Request) -> web.Response:
    """``GET /v1/cleanup`` — preview de workdirs/leases candidatos a remoção.

    Não deleta nada. Retorna o scan em JSON para o operador/painel TUI
    ver o que seria removido antes de confirmar.
    """
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    retention_days = int(
        os.environ.get(_CLEANUP_RETENTION_DAYS_ENV,
                       str(_CLEANUP_RETENTION_DAYS_DEFAULT))
    )
    try:
        scan = await asyncio.to_thread(
            _cleanup_scan, root, retention_days,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort endpoint
        logger.warning("cleanup preview erro: %s", exc)
        return web.json_response(
            {"error": f"cleanup scan falhou: {exc}"}, status=500,
        )
    return web.json_response({**scan, "dry_run": True,
                               "retention_days": retention_days})


async def cleanup_execute_handler(request: web.Request) -> web.Response:
    """``POST /v1/cleanup`` — executa remoção de workdirs/leases stale.

    Idempotente: rodar várias vezes não derruba workdirs ativos.
    Audit log gerado pelo servidor (INFO) para cada remoção individual.
    """
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    retention_days = int(
        os.environ.get(_CLEANUP_RETENTION_DAYS_ENV,
                       str(_CLEANUP_RETENTION_DAYS_DEFAULT))
    )
    try:
        result = await asyncio.to_thread(
            _do_cleanup, root, retention_days,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort endpoint
        logger.warning("cleanup execute erro: %s", exc)
        return web.json_response(
            {"error": f"cleanup falhou: {exc}"}, status=500,
        )
    logger.info(
        "cleanup via HTTP: %d leases + %d workdirs removidos, %.1f MiB liberados",
        len(result.get("removed_leases", [])),
        len(result.get("removed_workdirs", [])),
        result.get("freed_bytes", 0) / 1_048_576,
    )
    return web.json_response({**result, "retention_days": retention_days})


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def build_app(auth_token: Optional[str] = None) -> web.Application:
    """Monta a ``aiohttp.web.Application`` com as três rotas do contrato.

    Bearer middleware ativo por default (paridade com
    ``worker_server.build_app``). O ``auth_token`` opcional permite testes
    in-process passarem o token sem precisar mockar
    :func:`_read_auth_token`. Em produção (chamado pelo :func:`main`), o
    token vem de ``/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN``.

    ``client_max_size=512 KiB`` limita o body do ``/v1/dispatch`` — briefs
    de pipeline normalmente cabem em <50 KiB; o teto generoso (10x) ainda
    barra payloads anômalos que poderiam encher o PVC.
    """
    app = web.Application(
        middlewares=[_bearer_auth_mw],
        client_max_size=512 * 1024,
    )
    app["auth_token"] = auth_token or _read_auth_token()

    # Decisão #46 — workspace cleanup: startup hook + periodic task.
    # Garante que o PVC nunca acumula workdirs órfãos mesmo quando o pod
    # foi SIGKILLado (e o cleanup legacy do shutdown não rodou).
    _cleanup_root = Path(
        os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work")
    )

    async def _on_startup(_app: web.Application) -> None:
        try:
            await asyncio.to_thread(_cleanup_stale_workspaces, _cleanup_root)
        except Exception as exc:  # noqa: BLE001 — startup nunca falha por isso
            logger.warning("startup workspace cleanup raised: %s", exc)
        _app["_workspace_cleanup_task"] = asyncio.create_task(
            _workspace_cleanup_loop(_cleanup_root),
            name="workspace-cleanup",
        )

    async def _on_cleanup(_app: web.Application) -> None:
        task = _app.get("_workspace_cleanup_task")
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    app.router.add_get("/v1/health", health_handler)
    app.router.add_get("/v1/auth/start", auth_start_handler)
    app.router.add_get("/v1/auth/status", auth_status_handler)
    app.router.add_get("/v1/pod-status", pod_status_handler)
    app.router.add_post("/v1/dispatch", dispatch_handler)
    app.router.add_get("/v1/progress/{task_id}", progress_handler)
    app.router.add_get(
        "/v1/dispatches/{task_id}/resume-info", resume_info_handler,
    )
    # Observability endpoints (issue #347).
    app.router.add_get("/v1/sessions", sessions_list_handler)
    app.router.add_get("/v1/sessions/{task_id}/command", sessions_command_handler)
    app.router.add_get("/v1/sessions/{task_id}/chat", sessions_chat_handler)
    app.router.add_get("/v1/sessions/{task_id}/stdout", sessions_stdout_handler)
    app.router.add_post("/v1/sessions/{task_id}/kill", sessions_kill_handler)
    app.router.add_delete("/v1/sessions/{task_id}/cleanup", sessions_cleanup_handler)
    # Housekeeping endpoints (issue #408).
    app.router.add_get("/v1/cleanup", cleanup_preview_handler)
    app.router.add_post("/v1/cleanup", cleanup_execute_handler)
    return app


def main(passthrough: Optional[List[str]] = None) -> int:
    """Entry point chamado pelo ``wrapper.py`` no mode ``claude-worker``.

    ``passthrough`` é a lista de args extras passados após o nome do mode.
    Suporta ``--cleanup`` (roda garbage collection e sai, para o CronJob).
    """
    args = list(passthrough or [])

    # Mode --cleanup: roda GC e sai (usado pelo CronJob diário, issue #408).
    if "--cleanup" in args:
        return _run_cleanup_main()

    # Logging via deile.log_mgmt com dual-write (arquivo + stdout).
    # O bloco captura qualquer Exception (não apenas ImportError) para garantir
    # que falhas no setup do FileHandler (ex.: diretório de logs inacessível em
    # containers com filesystem restrito) não silencie os logs de dispatch —
    # o fallback basicConfig + StreamHandler(stdout) é suficiente para
    # `kubectl logs` capturar as linhas de dispatch_started/dispatch_completed.
    _log_level = os.environ.get("DEILE_CLAUDE_WORKER_LOG_LEVEL", "INFO")
    os.environ.setdefault("DEILE_LOG_LEVEL", _log_level)
    try:
        from deile.log_mgmt import init_logging
        init_logging(pod_name="claude-worker")
    except Exception:  # noqa: BLE001 — fallback intencional
        _handler = logging.StreamHandler(sys.stdout)
        _handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
        ))
        root_logger = logging.getLogger()
        root_logger.setLevel(_log_level)
        if not root_logger.handlers:
            root_logger.addHandler(_handler)
    # Garante que loggers deile.* propagam em nível INFO mesmo quando o
    # root já tinha handlers configurados antes deste bloco (ex.: aiohttp
    # internamente chama basicConfig antes do nosso main).
    logging.getLogger("deile").setLevel(_log_level)
    host = os.environ.get("DEILE_CLAUDE_WORKER_HOST", "0.0.0.0")
    port = int(os.environ.get("DEILE_CLAUDE_WORKER_PORT", "8767"))
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("could not create work root %s: %s", root, exc)
        return 78

    # Carrega o OAuth token do ``credentials.json`` (montado pelo
    # initContainer via Secret claude-credentials) e exporta como
    # ``ANTHROPIC_AUTH_TOKEN``. SEM isso o claude CLI roda como
    # "Not logged in" porque no Linux ele NÃO lê
    # ``~/.claude/credentials.json`` automaticamente (esse path é
    # convenção macOS — Linux só lê env vars).
    _load_oauth_token_into_env()

    # Startup housekeeping hook (issue #408) — varre PVC antes de aceitar
    # conexões. Conservador, idempotente, best-effort.
    _startup_cleanup(root)

    logger.info(
        "claude_worker_server listening on %s:%d, work root=%s", host, port, root,
    )
    app = build_app()
    web.run_app(app, host=host, port=port, print=lambda *_: None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
