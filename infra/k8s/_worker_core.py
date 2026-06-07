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
# Ciclo de repositório — clone + checkout do branch (genérico, plano §1.5)
# --------------------------------------------------------------------------- #
#
# O claude-worker delega o clone ao agente claude (o brief instrui ``gh repo
# clone``), com um safety-net pré-spawn (``_ensure_repo_cloned``). Os CLI workers
# da frota NÃO têm um agente confiável para fazer o setup de repo antes de
# começar: o subprocess precisa de um checkout real do branch em ``cwd`` ANTES
# de rodar. Estas funções fazem o trabalho que o plano §1.5 atribui ao wrapper:
# clonar o repo, criar/checkout do branch de trabalho. A identidade git e os
# tokens são wirados pelo ``wrapper.py`` (modo ``cli-worker``) no startup do pod.


async def _git(*argv: str, cwd: Path, timeout: int = 120) -> tuple[int, str, str]:
    """Roda ``git <argv>`` em *cwd*; devolve ``(rc, stdout, stderr)``.

    Em timeout, mata o processo e devolve ``rc=124`` (convenção do core).
    Nunca levanta — erros de spawn viram ``rc=-1`` com a exceção em stderr.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"git {' '.join(argv)} timed out after {timeout}s"
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", "replace"),
        err_b.decode("utf-8", "replace"),
    )


def repo_dir_for(workspace: Path) -> Path:
    """Diretório do checkout do repo dentro do workspace (``<workspace>/repo``)."""
    return workspace / "repo"


async def git_head(workspace: Path) -> Optional[str]:
    """SHA do HEAD do repo em ``<workspace>/repo`` (ou ``workspace``), ou ``None``.

    Procura ``<workspace>/repo/.git`` primeiro (layout padrão do clone); cai em
    ``workspace`` se este for o próprio repo. ``None`` quando não há repo git.
    """
    repo = repo_dir_for(workspace)
    cwd = repo if (repo / ".git").exists() else workspace
    if not (cwd / ".git").exists():
        return None
    rc, out, _ = await _git("rev-parse", "HEAD", cwd=cwd, timeout=30)
    if rc != 0:
        return None
    return out.strip() or None


async def git_branch_pushed(workspace: Path, branch: Optional[str]) -> bool:
    """True se *branch* existe no remote ``origin`` (push confirmado).

    Sem branch → não dá pra confirmar push → False (gate falha, conservador).
    """
    if not branch:
        return False
    repo = repo_dir_for(workspace)
    cwd = repo if (repo / ".git").exists() else workspace
    if not (cwd / ".git").exists():
        return False
    rc, out, _ = await _git(
        "ls-remote", "--heads", "origin", branch, cwd=cwd, timeout=60,
    )
    if rc != 0:
        return False
    return bool(out.strip())


async def ensure_repo_and_branch(
    workspace: Path,
    *,
    repo: str,
    branch: Optional[str],
    base_branch: str = "",
    clone_timeout: int = 300,
) -> tuple[bool, str]:
    """Clona *repo* em ``<workspace>/repo`` e faz checkout/criação de *branch*.

    É o trabalho de setup de repo que o plano §1.5 atribui ao wrapper para os
    CLI workers (o agente não é confiável para fazê-lo antes de começar). Idem-
    potente: se ``<workspace>/repo/.git`` já existe, só faz ``fetch`` e garante o
    branch. Best-effort no checkout do branch — se o branch remoto não existe,
    cria local a partir do ``base_branch`` (ou do default do clone).

    Pré-requisito: a identidade git + token já estão wirados no env do pod (pelo
    ``wrapper.py`` modo ``cli-worker``); aqui só usamos ``gh``/``git`` que os
    consomem.

    Args:
        workspace: workdir da task (pai de ``./repo``).
        repo: slug ``owner/repo`` (GitHub) ou ``group/.../project`` (GitLab).
        branch: branch de trabalho a checkout/criar; ``None`` deixa no default.
        base_branch: branch base de onde criar *branch* quando ele não existe
            no remote (ex.: ``main``); vazio → default do clone.
        clone_timeout: timeout (s) do clone.

    Returns:
        ``(ok, detail)`` — ``ok=True`` quando ``<workspace>/repo/.git`` existe ao
        fim. ``detail`` descreve o que aconteceu/falhou (para log/erro). Nunca
        levanta.
    """
    repo_path = repo_dir_for(workspace)
    if not repo:
        return False, "repo slug ausente — impossível clonar"

    # 1. Clone (ou reuso) do repositório.
    if not (repo_path / ".git").exists():
        gh = _which_forge_cli(repo)
        rc, _out, err = await _git_or_gh_clone(gh, repo, workspace, clone_timeout)
        if rc != 0 or not (repo_path / ".git").exists():
            return False, f"clone de {repo} falhou (rc={rc}): {err.strip()[:300]}"
    else:
        await _git("fetch", "--quiet", "origin", cwd=repo_path, timeout=120)

    # 2. Checkout/criação do branch de trabalho.
    if branch:
        ok, detail = await _checkout_branch(repo_path, branch, base_branch)
        if not ok:
            return False, detail
    return True, f"repo {repo} pronto em {repo_path}"


def _which_forge_cli(repo: str) -> str:
    """``glab`` para slugs GitLab (com grupo/subgrupo), ``gh`` caso contrário.

    Heurística simples alinhada ao resto da stack: um slug com mais de um ``/``
    (``group/subgroup/project``) é GitLab; ``owner/repo`` é GitHub. O default é
    ``gh`` (compat). O clone real cai em ``git clone`` com URL se o CLI faltar.
    """
    import shutil

    parts = [p for p in repo.split("/") if p]
    if len(parts) > 2 and shutil.which("glab"):
        return "glab"
    return shutil.which("gh") or "gh"


async def _git_or_gh_clone(
    forge_cli: str, repo: str, workspace: Path, timeout: int,
) -> tuple[int, str, str]:
    """Clona *repo* em ``<workspace>/repo`` via ``gh``/``glab``; fallback git URL.

    ``gh repo clone`` / ``glab repo clone`` configuram a auth automaticamente a
    partir do token wirado no env. Se o CLI não está disponível, cai em
    ``git clone https://<host>/<repo>.git`` (a auth vem do credential helper que
    o wrapper configurou em ``~/.git-credentials``).
    """
    base = forge_cli.rsplit("/", 1)[-1]
    if base in ("gh", "glab"):
        try:
            proc = await asyncio.create_subprocess_exec(
                forge_cli, "repo", "clone", repo, "repo",
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            return -1, "", f"{type(exc).__name__}: {exc}"
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"clone timed out after {timeout}s"
        return (
            proc.returncode or 0,
            out_b.decode("utf-8", "replace"),
            err_b.decode("utf-8", "replace"),
        )
    # Fallback: git clone por URL (auth via credential.helper=store do wrapper).
    host = os.environ.get("DEILE_GITHUB_HOST", "").strip() or "github.com"
    url = f"https://{host}/{repo}.git"
    return await _git("clone", url, "repo", cwd=workspace, timeout=timeout)


async def _checkout_branch(
    repo_path: Path, branch: str, base_branch: str,
) -> tuple[bool, str]:
    """Faz checkout de *branch* em *repo_path*; cria a partir de *base_branch*.

    Tenta, em ordem: (1) checkout de um branch local/remoto existente (após
    ``fetch``); (2) criar um branch novo a partir do ``base_branch`` (ou do HEAD
    atual). Best-effort — devolve ``(False, motivo)`` só se nem criar funcionar.
    """
    await _git("fetch", "--quiet", "origin", cwd=repo_path, timeout=120)
    # 1. Branch já existe (local ou remoto rastreável)?
    rc, _o, _e = await _git("checkout", branch, cwd=repo_path, timeout=60)
    if rc == 0:
        return True, f"checkout de {branch} (existente)"
    # 2. Cria branch novo a partir da base.
    if base_branch:
        await _git("checkout", base_branch, cwd=repo_path, timeout=60)
    rc, _o, err = await _git("checkout", "-b", branch, cwd=repo_path, timeout=60)
    if rc == 0:
        return True, f"branch {branch} criado a partir de {base_branch or 'HEAD'}"
    return False, f"checkout/criação do branch {branch} falhou: {err.strip()[:200]}"


async def git_fallback_commit(
    workspace: Path, branch: Optional[str], *, message: str,
) -> bool:
    """Commita o working tree sujo do repo (fallback ``brief_driven``).

    Usado quando o agente terminou mas NÃO commitou (``brief_driven`` exige
    commit+push; o gate detecta a ausência). Faz ``git add -A`` + ``git commit``;
    devolve True se um commit novo foi criado, False se não havia nada a commitar
    ou o commit falhou. Não faz push — o caller chama :func:`git_push` em seguida.
    """
    repo = repo_dir_for(workspace)
    cwd = repo if (repo / ".git").exists() else workspace
    if not (cwd / ".git").exists():
        return False
    await _git("add", "-A", cwd=cwd, timeout=60)
    # ``git diff --cached --quiet`` rc=1 ⇔ há staged changes a commitar.
    rc, _o, _e = await _git("diff", "--cached", "--quiet", cwd=cwd, timeout=30)
    if rc == 0:
        return False  # nada staged → nada a commitar
    rc, _o, _e = await _git("commit", "-m", message, cwd=cwd, timeout=60)
    return rc == 0


async def git_push(workspace: Path, branch: Optional[str]) -> tuple[bool, str]:
    """``git push -u origin <branch>`` no repo do workspace.

    Devolve ``(ok, detail)``. Sem branch → ``(False, motivo)`` (não dá pra
    pushar um branch anônimo de forma determinística). Nunca levanta.
    """
    if not branch:
        return False, "sem branch — push não executado"
    repo = repo_dir_for(workspace)
    cwd = repo if (repo / ".git").exists() else workspace
    if not (cwd / ".git").exists():
        return False, "sem repo git — push não executado"
    rc, _o, err = await _git(
        "push", "-u", "origin", branch, cwd=cwd, timeout=180,
    )
    if rc == 0:
        return True, f"push de {branch} OK"
    return False, f"push de {branch} falhou (rc={rc}): {err.strip()[:300]}"


# --------------------------------------------------------------------------- #
# Cleanup de workdirs/leases stale do PVC (genérico — plano §1.13)
# --------------------------------------------------------------------------- #


def startup_cleanup(
    root: Path,
    *,
    retention_days: int = 7,
    has_session: Optional[callable] = None,
    alive_pods: Optional[set] = None,
) -> dict:
    """Remove leases stale e workdirs abandonados sob *root* (genérico).

    Extraído do ``claude_worker_server.startup_cleanup`` (plano A1/§1.13) para
    ser reusado por qualquer worker com PVC. Idempotente e conservador: nunca
    remove workdir com lease vivo nem modificado recentemente.

    Args:
        root: raiz dos workdirs (um diretório por task_id hex-16).
        retention_days: workdir mais velho que isto (mtime) é removido.
        has_session: predicado ``(workdir: Path) -> bool`` que decide se o
            workdir tem trabalho persistido a preservar (ex.: sessão JSONL do
            claude). ``None`` → todo workdir sem lease vivo é elegível por idade
            (CLI workers sem resume não acumulam sessão — removem só por idade).
        alive_pods: conjunto de pods vivos (recuperação proativa de lease cujo
            dono morreu); ``None`` desativa essa checagem.

    Returns:
        dict com ``leases_removed``, ``workdirs_removed``, ``bytes_freed``,
        ``errors`` — para audit log.
    """
    import shutil

    retention_cutoff = time.time() - (retention_days * 86400)
    leases_removed = workdirs_removed = bytes_freed = 0
    errors: list = []

    if not root.is_dir():
        return {
            "leases_removed": 0, "workdirs_removed": 0,
            "bytes_freed": 0, "errors": ["work root not found"],
        }

    try:
        candidates = [
            d for d in root.iterdir()
            if d.is_dir() and TASK_ID_RE.fullmatch(d.name)
        ]
    except OSError as exc:
        return {
            "leases_removed": 0, "workdirs_removed": 0,
            "bytes_freed": 0, "errors": [f"cannot list work root: {exc}"],
        }

    for workdir in candidates:
        lease_path = workdir / ".lease.json"
        lease_present = lease_path.exists()
        stale = lease_is_stale(lease_path, alive_pods=alive_pods) if lease_present else False

        if lease_present and not stale:
            continue  # lease vivo → em uso, nunca toca

        # Captura o mtime ANTES de remover o lease: unlink do ``.lease.json``
        # atualiza o mtime do diretório-pai, e isso mascararia o critério de
        # idade (workdir velho pareceria recém-modificado).
        try:
            last_mod = workdir.stat().st_mtime
        except OSError as exc:
            errors.append(f"stat {workdir}: {exc}")
            continue

        if lease_present and stale:
            try:
                lease_path.unlink()
                leases_removed += 1
            except OSError as exc:
                errors.append(f"lease unlink {lease_path}: {exc}")

        remove_reason: Optional[str] = None
        if has_session is not None and not has_session(workdir):
            remove_reason = "no session to preserve"
        elif has_session is None and last_mod < retention_cutoff:
            remove_reason = f"older than {retention_days}d"
        elif has_session is not None and last_mod < retention_cutoff:
            remove_reason = f"older than {retention_days}d"

        if remove_reason:
            size = dir_bytes(workdir)
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
        "startup_cleanup: leases=%d workdirs=%d freed=%d errors=%d",
        leases_removed, workdirs_removed, bytes_freed, len(errors),
    )
    return {
        "leases_removed": leases_removed,
        "workdirs_removed": workdirs_removed,
        "bytes_freed": bytes_freed,
        "errors": errors,
    }


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
    "repo_dir_for",
    "git_head",
    "git_branch_pushed",
    "ensure_repo_and_branch",
    "git_fallback_commit",
    "git_push",
    "startup_cleanup",
    "SubprocessResult",
    "run_subprocess_with_progress",
    "make_bearer_auth_mw",
    "RateLimiter",
]
