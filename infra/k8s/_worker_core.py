#!/usr/bin/env python3
"""_worker_core — núcleo agnóstico-de-CLI compartilhado pelos workers headless.

Extraído do ``claude_worker_server.py`` (Fase A1) para que ``cli_worker_server``
e ``claude_worker_server`` compartilhem a maquinaria sem duplicação. Independente
do CLI concreto (claude, opencode, aider, …):

* Lease por workspace (``.lease.json``): aquisição atômica multi-réplica,
  heartbeat, release e marcação do PID do subprocess.
* Liveness: ``pid_alive`` (POSIX ``kill -0``) e ``lease_is_stale`` (TTL+PID+pod).
* Subprocess one-shot com persistência de stdout/stderr no PVC
  (``/v1/progress/{task_id}``) e timeout que mata o processo.
* Helpers HTTP: Bearer middleware com whitelist e rate-limiter sliding-window.
* Filesystem: ``dir_bytes`` e validação de ``task_id``.

Constantes de TTL/heartbeat são passadas *por parâmetro* para que os servidores
concretos possam monkeypatchá-las nos testes sem afetar este módulo. O
específico-do-CLI (argv, parsing de saída, auth/OAuth) fica no servidor concreto.
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

#: Exatamente 16 hex chars — rejeita qualquer outro formato para impedir path
#: traversal pela URL ou leitura de arquivos arbitrários no PVC.
TASK_ID_RE = re.compile(r"[0-9a-f]{16}")

#: Servidores concretos sobrescrevem via suas constantes monkeypatcháveis,
#: passando o valor por parâmetro (não lido diretamente daqui nos testes).
DEFAULT_LEASE_TTL_S: int = 30
DEFAULT_LEASE_HEARTBEAT_S: int = 5


# --------------------------------------------------------------------------- #
# Utilidades genéricas
# --------------------------------------------------------------------------- #


def validate_task_id_for_path(task_id: str) -> bool:
    """Defesa contra path traversal: task_id deve ser hex 16-char."""
    return bool(TASK_ID_RE.fullmatch(task_id)) if task_id else False


# --------------------------------------------------------------------------- #
# Enforcement da allowlist de repositórios no dispatch (issue #639)
# --------------------------------------------------------------------------- #
#
# A allowlist regex (ConfigMap ``claude-worker-allowed-repos``, montada em
# ``$DEILE_CLAUDE_ALLOWED_REPOS_FILE``) era vendida no threat model como a
# mitigação primária de prompt-injection→exfiltração, mas NÃO havia enforcement
# em runtime: ``wrapper.py`` só fazia fail-fast no startup e os servidores
# clonavam o ``repo_slug`` do payload sem reverificar. Este bloco é a FONTE
# ÚNICA de verificação por-request, reusada por ``cli_worker_server`` e
# ``claude_worker_server`` (os dois caminhos de clone).
#
# Fonte canônica dos patterns = o ConfigMap ``allowed_repos.regex`` (URLs
# completas, uma regex por linha). NÃO usamos a fonte divergente
# ``deilebot.yaml clonable_repos`` (bug apontado pela issue #639).

#: Path default do ConfigMap; cada worker sobrescreve via
#: ``DEILE_CLAUDE_ALLOWED_REPOS_FILE`` no manifest (claude → ``/etc/claude-worker``,
#: cli → ``/etc/cli-worker``). Mantido em sincronia com ``wrapper.CLAUDE_ALLOWED_REPOS_FILE``.
ALLOWED_REPOS_FILE_DEFAULT = "/etc/claude-worker/allowed_repos.regex"

#: Slug forge-agnóstico: ``owner/repo`` (GitHub) ou ``group/(sub/)*project``
#: (GitLab). Apenas ``[A-Za-z0-9._-]`` por componente, ``/`` como separador,
#: ao menos dois componentes. Ancorado: bloqueia ``..``, ``/`` líder/final,
#: ``//``, espaços, ``@``/``:`` (host/auth smuggling), ``\\`` e backslash.
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+$")

#: Componentes proibidos mesmo dentro do alfabeto permitido (defesa redundante
#: contra path traversal por componente exato ``.``/``..``).
_FORBIDDEN_SLUG_PARTS = frozenset({"", ".", ".."})


def allowed_repos_file_path() -> Path:
    """Resolve o path do ConfigMap de allowlist (env var > default).

    Não lê o arquivo — apenas resolve o caminho. Honra
    ``DEILE_CLAUDE_ALLOWED_REPOS_FILE`` (cada worker o define no manifest e os
    testes o apontam para um tmp file).
    """
    return Path(
        os.environ.get(
            "DEILE_CLAUDE_ALLOWED_REPOS_FILE", ALLOWED_REPOS_FILE_DEFAULT,
        )
    )


def load_allowed_repo_patterns() -> tuple[list[re.Pattern], Optional[str]]:
    """Carrega as regexes da allowlist para uso POR-REQUEST (não-exiting).

    Diferente de ``wrapper._load_allowed_repo_patterns`` (que faz ``sys.exit``
    no startup), aqui devolvemos ``(patterns, error)`` para o handler decidir o
    HTTP. ``error`` é ``None`` quando a allowlist é válida e não-vazia; caso
    contrário traz o motivo (arquivo ausente, vazio, regex inválida, ou erro de
    leitura). Postura **fail-closed**: qualquer ``error`` não-``None`` deve
    bloquear o dispatch — o startup já garante allowlist válida em produção
    (``wrapper`` fail-fast), então um erro aqui só ocorre em drift/teste.

    Nunca levanta.
    """
    path = allowed_repos_file_path()
    try:
        if not path.exists():
            return [], f"allowlist de repos ausente: {path}"
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"falha ao ler allowlist {path}: {exc}"
    patterns: list[re.Pattern] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            patterns.append(re.compile(stripped))
        except re.error as exc:
            return [], f"regex inválida na allowlist {path}: {stripped!r}: {exc}"
    if not patterns:
        return [], f"allowlist vazia (sem linhas não-comentário): {path}"
    return patterns, None


def normalize_repo_slug(repo_slug: str) -> Optional[str]:
    """Normaliza e valida o slug forge-agnóstico do payload de dispatch.

    Aceita ``owner/repo`` (GitHub) e ``group/(sub/)*project`` (GitLab). Faz:

      - ``strip()`` de espaços nas pontas;
      - remoção de UM sufixo ``.git`` final (forma que ``gh``/``glab`` toleram);
      - rejeição de qualquer slug que case host/auth/traversal (``..``, ``//``,
        ``@``, ``:``, backslash, ``/`` líder/final, espaço interno).

    Returns o slug canônico (sem ``.git``) ou ``None`` quando inválido — o
    caller trata ``None`` como REPO_NOT_ALLOWED (fail-closed).
    """
    if not repo_slug:
        return None
    candidate = repo_slug.strip()
    if not candidate:
        return None
    # Sufixo ``.git`` é removido ANTES da validação (gh/glab o toleram e o clone
    # canônico não o carrega no slug). Apenas UM sufixo, case-sensitive.
    if candidate.endswith(".git"):
        candidate = candidate[: -len(".git")]
    # Rejeita qualquer caractere fora do alfabeto slug logo de cara (bloqueia
    # ``@``, ``:``, ``\``, espaço, ``~``, etc. — host/auth/url smuggling).
    if not _REPO_SLUG_RE.fullmatch(candidate):
        return None
    parts = candidate.split("/")
    if any(part in _FORBIDDEN_SLUG_PARTS for part in parts):
        return None
    return candidate


def _canonical_clone_urls(slug: str) -> list[str]:
    """URLs canônicas que os caminhos de clone realmente produzem para *slug*.

    Espelha ``_git_or_gh_clone`` (``gh``/``glab repo clone`` → ``https://<host>/<slug>``,
    fallback ``git clone https://<host>/<slug>.git``) e o ``_ensure_repo_cloned``
    do claude-worker (``gh repo clone`` → host GitHub). Cobrimos as formas
    https (com e sem ``.git``) e ssh para os hosts GitHub e GitLab configurados.
    O slug já vem normalizado (sem ``.git``, sem host).
    """
    gh_host = (os.environ.get("DEILE_GITHUB_HOST", "").strip() or "github.com")
    gl_host = (os.environ.get("DEILE_GITLAB_HOST", "").strip() or "gitlab.com")
    # ``DEILE_GITHUB_HOST`` aceita CSV (GHES multi-host, decisão #42).
    hosts = []
    for raw in (*gh_host.split(","), gl_host):
        h = raw.strip()
        if h and h not in hosts:
            hosts.append(h)
    urls: list[str] = []
    for host in hosts:
        urls.append(f"https://{host}/{slug}")
        urls.append(f"https://{host}/{slug}.git")
        urls.append(f"git@{host}:{slug}")
        urls.append(f"git@{host}:{slug}.git")
    return urls


def repo_slug_allowed(
    repo_slug: str, patterns: Iterable[re.Pattern],
) -> tuple[bool, Optional[str]]:
    """Decide se *repo_slug* casa a allowlist (fonte canônica de match).

    Normaliza o slug e gera as URLs canônicas de clone; exige que ao menos UMA
    delas case ao menos UMA regex da allowlist (``re.fullmatch`` — os patterns
    do ConfigMap já são ancorados ``^...$``, mas usamos ``fullmatch`` para não
    depender da âncora e impedir match parcial).

    Returns ``(allowed, normalized_slug)``. ``allowed=False`` cobre slug
    malformado (``normalized_slug=None``) e slug bem-formado que não casa
    nenhuma regex (``normalized_slug`` preenchido para o log/erro).
    """
    normalized = normalize_repo_slug(repo_slug)
    if normalized is None:
        return False, None
    pattern_list = list(patterns)
    for url in _canonical_clone_urls(normalized):
        for pat in pattern_list:
            if pat.fullmatch(url):
                return True, normalized
    return False, normalized


def check_repo_allowed(repo_slug: str) -> tuple[bool, str, Optional[str]]:
    """Verificação completa por-request: carrega patterns + casa o slug.

    FONTE ÚNICA chamada pelos ``dispatch_handler`` dos dois servidores, ANTES de
    qualquer clone. Fail-closed: allowlist ausente/vazia/inválida → bloqueia.

    Returns ``(allowed, reason, normalized_slug)``:
      - ``allowed=True``  → o slug pode ser clonado (``reason`` vazio).
      - ``allowed=False`` → bloquear com 403 REPO_NOT_ALLOWED; ``reason`` é uma
        mensagem segura (sem segredo, sem token) para log/resposta.
    """
    patterns, load_err = load_allowed_repo_patterns()
    if load_err is not None:
        # Fail-closed: sem allowlist confiável, nada é permitido.
        return False, f"allowlist indisponível ({load_err})", None
    allowed, normalized = repo_slug_allowed(repo_slug, patterns)
    if allowed:
        return True, "", normalized
    if normalized is None:
        return False, "repo slug malformado ou inseguro", None
    return False, f"repo {normalized!r} fora da allowlist", normalized


# --------------------------------------------------------------------------- #
# Classificação de erro de provider (anti-sangria de custo — issue #445)
# --------------------------------------------------------------------------- #
#
# CLIs headless frequentemente saem com rc=0 mesmo quando o provider corta a task
# (402/429/5xx) — o adapter leria "conclusão limpa" e o re-dispatch re-gastaria
# todos os tokens do zero (comprovado: opencode #629 cortado por 402).
# ``classify_provider_error`` é a FONTE ÚNICA de detecção: quando casa, o adapter
# retorna ``ok=False`` → o pipeline RETOMA em vez de re-gastar.

#: Padrões de corte por provider, em ordem de prioridade (o mais específico ganha).
_PROVIDER_ERROR_PATTERNS: tuple = (
    # 402 / crédito esgotado — o corte mais comum em produção.
    ("INSUFFICIENT_CREDIT", (
        r"\b402\b",
        r"payment\s+required",
        r"insufficient\s+(?:credit|funds|balance|quota)",
        r"insufficient_quota",
        r"out\s+of\s+credit",
        r"add\s+(?:more\s+)?credits?",
        r"billing\s+(?:hard\s+)?limit",
        r"exceeded\s+your\s+current\s+quota",
    )),
    # 429 / rate-limit — retomável após espera.
    ("RATE_LIMIT", (
        r"\b429\b",
        r"rate[\s_-]?limit",
        r"too\s+many\s+requests",
        r"overloaded",
        r"\boverloaded_error\b",
    )),
    # 5xx — erro transitório do servidor do provider.
    ("PROVIDER_ERROR", (
        r"\b5\d{2}\b\s*(?:error|status|internal|server|bad\s+gateway|"
        r"service\s+unavailable|gateway\s+timeout)",
        r"internal\s+server\s+error",
        r"service\s+unavailable",
        r"bad\s+gateway",
        r"gateway\s+time(?:d\s+)?out",
        r"api\s+error\s*:?\s*5\d{2}",
    )),
    # Conexão/rede — transitório.
    ("PROVIDER_CONN", (
        r"connection\s+(?:error|reset|refused|aborted|timed?\s*out)",
        r"connection\s+closed",
        r"econnreset",
        r"etimedout",
        r"econnrefused",
        r"network\s+error",
        r"socket\s+hang\s*up",
    )),
)

_PROVIDER_ERROR_COMPILED: tuple = tuple(
    (code, tuple(re.compile(p, re.IGNORECASE) for p in pats))
    for code, pats in _PROVIDER_ERROR_PATTERNS
)


def classify_provider_error(text: str) -> Optional[str]:
    """Classifica *text* (stdout+stderr) num ``error_code`` de provider, ou None.

    Prioridade: crédito > rate-limit > 5xx > conexão (crédito esgotado é o corte
    mais caro; checado primeiro). Peça central do anti-sangria (issue #445): um
    code retornado aqui faz o adapter marcar ``ok=False``, levando o pipeline a
    RETOMAR em vez de re-gastar do zero. Best-effort — só regex pré-compiladas.
    """
    if not text:
        return None
    for code, patterns in _PROVIDER_ERROR_COMPILED:
        for rx in patterns:
            if rx.search(text):
                return code
    return None


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
# Cost ledger JSONL — engine compartilhada (claude_worker + cli_worker)
# --------------------------------------------------------------------------- #
#
# Cada servidor decide o PATH e a CHAVE de dedup do ledger (claude usa
# ``session_id``; CLI fleet usa ``task_id``), mas o engine de I/O é o mesmo:
# scan-linha-a-linha tolerante a corrupção parcial + append atômico append-only.
# Issue #445 — NUNCA podar transcripts sem antes ter colhido o custo para cá.


def ledger_harvested_ids(ledger_path: Path, *, key: str) -> set:
    """Conjunto de valores já presentes no ledger sob ``key`` (para dedup do harvest).

    Lê o ledger linha-a-linha como JSONL, pula linhas vazias, JSON malformado
    ou registros sem o campo ``key``. Retorna ``set()`` se o ledger ainda não
    existe ou está ilegível (``OSError``). Não levanta — chamada idempotente.
    """
    ids: set = set()
    if not ledger_path.exists():
        return ids
    try:
        with open(ledger_path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                val = rec.get(key) if isinstance(rec, dict) else None
                if val:
                    ids.add(val)
    except OSError:
        pass
    return ids


def ledger_append_record(
    ledger_path: Path, record: dict, *, ensure_ascii: bool = False,
) -> int:
    """Anexa ``record`` ao ledger como uma linha JSON. Retorna bytes escritos.

    Cria o diretório-pai se necessário, abre em modo ``a`` (append-only) e
    escreve uma linha terminada com ``\\n``. ``ensure_ascii`` controla escape
    de unicode (cli_worker_server prefere False — emojis legíveis; o
    claude_worker_server pré-#614 usava True — preservado por parâmetro).
    """
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=ensure_ascii) + "\n"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(line)
    return len(line.encode("utf-8"))


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
        return None

    lease = {
        "pod": pod_id,
        "pid": os.getpid(),
        "started_at": now,
        "heartbeat_at": now,
    }
    # Gravados antes do spawn para que o scan de dedup cross-workdir já os
    # enxergue: uma 2ª dispatch fresca para o mesmo channel é recusada enquanto
    # o 1º subprocess ainda está vivo.
    if channel:
        lease["channel"] = channel
    if session_id:
        lease["session_id"] = session_id

    def _write_and_confirm() -> Optional[dict]:
        tmp = workspace / f".lease.tmp.{pod_id}"
        try:
            tmp.write_text(json.dumps(lease), encoding="utf-8")
            tmp.rename(lease_path)
            # Re-lê para confirmar vitória na corrida (TOCTOU: outro pod pode
            # ter feito rename simultaneamente).
            confirmed = json.loads(lease_path.read_text(encoding="utf-8"))
            if confirmed.get("pod") != pod_id:
                return None
            return confirmed
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("lease write/confirm falhou para %s: %s", workspace, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    return await asyncio.to_thread(_write_and_confirm)


async def update_lease_subprocess_pid(
    lease_path: Path, subprocess_pid: Optional[int]
) -> None:
    """Grava/limpa ``claude_pid`` no lease — PID do subprocess do CLI (não o wrapper).

    Sem este campo, o heartbeat mantém o mtime sempre recente e não dá pra saber
    se o CLI está rodando de verdade. Com ele, ``pid_alive(claude_pid)`` é o
    ground truth. Best-effort: lease ilegível é logado e ignorado.

    Campo mantido como ``claude_pid`` por compatibilidade com o painel e
    ``/v1/pod-status``; semanticamente é o PID do CLI do worker, qualquer que seja.
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
    # Proativo: pod dono sumiu do conjunto → stale independente do heartbeat.
    if alive_pods is not None:
        pod = data.get("pod", "")
        if pod and pod not in alive_pods:
            return True
    heartbeat_at = data.get("heartbeat_at", 0)
    if (time.time() - float(heartbeat_at)) < ttl_s:
        return False
    pid = data.get("pid")
    if pid and pid_alive(int(pid)):
        return False  # TTL expirado mas PID ainda vivo — fail-safe conservador
    return True


# --------------------------------------------------------------------------- #
# Ciclo de repositório — clone + checkout do branch (genérico, plano §1.5)
# --------------------------------------------------------------------------- #
#
# CLI workers não têm agente confiável para setup de repo: o subprocess precisa
# de um checkout real ANTES de rodar. Identidade git + tokens são configurados
# pelo ``wrapper.py`` (modo ``cli-worker``) no startup do pod.


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

    Idempotente: se ``<workspace>/repo/.git`` já existe, só faz ``fetch``. Se o
    branch remoto não existe, cria a partir do ``base_branch`` (ou HEAD). Pré-
    requisito: identidade git + token já configurados pelo ``wrapper.py``.

    Returns:
        ``(ok, detail)`` — ``ok=True`` quando ``<workspace>/repo/.git`` existe ao
        fim. Nunca levanta.
    """
    repo_path = repo_dir_for(workspace)
    if not repo:
        return False, "repo slug ausente — impossível clonar"

    if not (repo_path / ".git").exists():
        gh = _which_forge_cli(repo)
        rc, _out, err = await _git_or_gh_clone(gh, repo, workspace, clone_timeout)
        if rc != 0 or not (repo_path / ".git").exists():
            return False, f"clone de {repo} falhou (rc={rc}): {err.strip()[:300]}"
    else:
        await _git("fetch", "--quiet", "origin", cwd=repo_path, timeout=120)

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

    Fallback: ``git clone https://<host>/<repo>.git`` quando o CLI não está
    disponível (auth via credential.helper=store configurado pelo wrapper).
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
    rc, _o, _e = await _git("checkout", branch, cwd=repo_path, timeout=60)
    if rc == 0:
        return True, f"checkout de {branch} (existente)"
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
    rc, _o, _e = await _git("diff", "--cached", "--quiet", cwd=cwd, timeout=30)
    if rc == 0:
        return False  # rc=0 ⇔ nada staged
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
    """Remove leases stale e workdirs abandonados sob *root*. Idempotente.

    ``has_session``: predicado ``(workdir) -> bool`` p/ preservar sessões ativas
    (ex.: JSONL do claude). ``None`` → elegível só por idade (CLI workers sem
    resume). ``alive_pods``: recuperação proativa de leases cujo pod morreu.

    Returns: dict com ``leases_removed``, ``workdirs_removed``, ``bytes_freed``,
    ``errors``.
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
            continue

        # mtime capturado ANTES de remover o lease: unlink do ``.lease.json``
        # atualizaria o mtime do diretório-pai e mascararia o critério de idade.
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

    ``stdout``/``stderr`` são strings completas; truncagem por bytes fica no
    handler, próxima do contrato de resposta JSON.
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

    Saída vai para ``<root>/.progress/<task_id>.{stdout,stderr}.log`` —
    consumida pelo ``/v1/progress/{task_id}`` no painel. Timeout → rc=124.

    Quando ``lease_path`` é informado, grava o PID do subprocess no lease ao
    spawnear e limpa ao terminar — o observador distingue "lease vivo por
    heartbeat" de "CLI rodando agora" sem varrer ``/proc``.

    ``root`` é omitível; default via ``DEILE_CLAUDE_WORKER_ROOT``.
    """
    start = time.monotonic()

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

    # Best-effort: falha na escrita não derruba o dispatch.
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
        self._buckets: dict = {}  # {actor: [monotonic timestamps]}
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
    "classify_provider_error",
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
