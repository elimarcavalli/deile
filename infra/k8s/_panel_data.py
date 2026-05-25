"""Data providers do painel TUI — kubectl, gh, SQLite, ps, tail — com cache TTL.

Cada provider expõe um `get()` que devolve um dataclass tipado e fica
silenciosamente vazio quando a fonte está indisponível (cluster down, gh
sem auth, DB ausente). A camada de view (`_panel.py`) lê dos `get()` sem
saber se veio do cluster, do host local ou do fallback.

Cache: cada provider tem `Cache[T]` com TTL próprio (1-300s). `get(force=True)`
re-busca; chamadas posteriores dentro do TTL retornam o valor cacheado.
O `BackgroundRefresher` (em `_panel.py`) chama `maybe_refresh` em loop
para manter os caches frescos sem bloquear a UI.

`RuntimeContext` (no topo) carrega: namespace, deploy names, paths e
flags k8s/local — permite o painel rodar:
- **K8s** (defaults): equivalente ao comportamento legado.
- **Local**: `python3 deile.py` no host — `LocalProcessesProvider`,
  `LocalLogsProvider` (tail de `~/.deile/logs/deile.log`),
  `LocalAuditProvider` (tail de `security_audit.log`).
- **Híbrido**: detecta ambos e renderiza lado a lado.
- **Demo** (`--demo`): bypassa fontes reais, mostra mocks.

Fontes:
- pods + recursos:  `kubectl -n <ns> get pods -o json`
- pipeline:         `kubectl logs deploy/<pipeline-deploy> --tail=200 --timestamps`
- worker (por pod): `kubectl logs <pod> --tail=200 --timestamps`
- issues + PRs:     `gh api /repos/<repo>/issues?state=open`
- custos:           `<usage_db>` (SQLite — default `~/.deile/db/usage.db`)
- processos locais: `ps -axo pid,pcpu,rss,etime,command`
- logs locais:      tail-from-end de `<logs_dir>/deile.log` (até 64KB)
- audit local:      tail-from-end de `<logs_dir>/security_audit.log`
- instâncias:       `<runtime_dir>/*.json` (state files publicados por instâncias
                    DEILE rodando no host — issue #303)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent import futures
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

NS = "deile"
# Tamanho máximo de log (bytes) que parsers carregam em memória — protege
# contra pods com saídas multi-MB. Aplicado antes de `splitlines()`.
MAX_LOG_BYTES = 256_000
# Slug seguro para kubectl set env: alfanum + [._:/-], 1-128 chars, sem
# espaços, sem controle, sem '=' nem '\n'. Rejeita injeção em argv.
_MODEL_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$")
# Deployments cuja env DEILE_PREFERRED_MODEL é modificável pelo painel.
# Argumento `deployment` de `set_preferred_model` vai direto em argv —
# whitelist impede qualquer outro alvo (acidental ou malicioso).
_ALLOWED_DEPLOYMENTS = frozenset({"deile-worker", "deile-pipeline"})


def _detect_default_repo() -> str:
    """Tenta derivar `owner/repo` de `git remote get-url origin`.

    Fallback para `elimarcavalli/deile` se git ausente, sem origin, ou
    formato não reconhecido. Roda uma única vez no import — silencioso.
    """
    fallback = "elimarcavalli/deile"
    git = shutil.which("git")
    if git is None:
        return fallback
    try:
        out = subprocess.run(
            [git, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2.0,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    if out.returncode != 0:
        return fallback
    url = out.stdout.strip()
    # github.com[:/]owner/repo(.git)?  — cobre https e ssh.
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else fallback


REPO_DEFAULT = _detect_default_repo()
USAGE_DB = Path.home() / ".deile" / "db" / "usage.db"
LOGS_DIR = Path.home() / ".deile" / "logs"
SESSIONS_DIR = Path.home() / ".deile" / "sessions"
# Diretório onde instâncias DEILE publicam seu `<instance_id>.json` (state
# file por processo, atualizado em volta de tools/heartbeat — issue #303).
# Override-able via `DEILE_RUNTIME_DIR` env var; `LocalInstancesProvider`
# resolve a env no construtor (não no import) para os testes poderem
# manipular `monkeypatch.setenv` antes da instanciação.
RUNTIME_DIR = Path.home() / ".deile" / "run"


# ===== runtime context ======================================================
#
# `RuntimeContext` é a fonte única de verdade da configuração de execução
# do painel: namespace k8s, deployment names, paths locais e modo
# (k8s/local/híbrido/demo). Substitui as constantes `NS`/`DEPLOY` que
# antes eram hardcoded — os providers continuam tendo defaults, mas o
# `PanelData.from_context(...)` injeta os valores efetivos.
#
# A detecção de modo é lazy (em `k8s_available` e `local_available`) — a
# mesma instância pode mudar de comportamento se o operador subir o
# cluster ou matar o processo local enquanto o painel está aberto.

# Padrões que marcam um processo do host como "DEILE-like". Ordem
# importa: o mais específico primeiro (bot/pipeline antes do CLI
# genérico `deile.py`). O fallback `_LOCAL_PROCESS_RE` aceita qualquer
# `python ... (deile|deilebot)` — útil para detecção em `local_available`.
_LOCAL_PROCESS_PATTERNS: List[tuple] = [
    (re.compile(r"-m\s+deilebot(\b|\.)", re.IGNORECASE), "local-bot"),
    (re.compile(r"deilebot.*\brun\b", re.IGNORECASE), "local-bot"),
    (re.compile(r"-m\s+deile\.orchestration", re.IGNORECASE), "local-pipeline"),
    (re.compile(r"-m\s+deile\.pipeline", re.IGNORECASE), "local-pipeline"),
    (re.compile(r"(?:^|/| )deile\.py(?:\s|$)"), "local-deile"),
    (re.compile(r"-m\s+deile(\b|\.)", re.IGNORECASE), "local-deile"),
]
_LOCAL_PROCESS_RE = re.compile(r"python.*\b(deile|deilebot)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RuntimeContext:
    """Configuração de execução do painel — detectada ou explícita.

    Resolve namespace, deployment names, paths e modo (k8s/local/demo) num
    objeto imutável que os providers consomem. Defaults batem com o
    layout `infra/k8s/manifests/` (namespace `deile`, deployments
    `deile-pipeline`/`deile-worker`/`deilebot`/`deile-shell`).

    Use `RuntimeContext.detect(**overrides)` para construir aplicando
    overrides do CLI (`--namespace`, `--pipeline-deploy`, etc).
    """

    namespace: str = "deile"
    pipeline_deploy: str = "deile-pipeline"
    worker_deploy: str = "deile-worker"
    bot_deploy: str = "deilebot"
    shell_deploy: str = "deile-shell"
    repo: str = ""
    usage_db: Path = field(default_factory=lambda: USAGE_DB)
    logs_dir: Path = field(default_factory=lambda: LOGS_DIR)
    sessions_dir: Path = field(default_factory=lambda: SESSIONS_DIR)
    cluster_label: str = "rancher-desktop (k3s)"
    image_label: str = "deile-stack:local"
    k8s_force: bool = False
    local_force: bool = False
    demo: bool = False

    @classmethod
    def detect(cls, **overrides: Any) -> "RuntimeContext":
        """Constrói o contexto, resolvendo defaults quando overrides faltam."""
        repo = overrides.pop("repo", None) or _detect_default_repo()
        return cls(repo=repo, **overrides)

    @property
    def k8s_available(self) -> bool:
        """`True` se kubectl está no PATH/Rancher e --local-only não foi setado."""
        if self.local_force or self.demo:
            return False
        return kubectl_bin() is not None

    @property
    def local_available(self) -> bool:
        """`True` se há vestígios de DEILE no host (log, DB ou processo)."""
        if self.k8s_force or self.demo:
            return False
        if self.logs_dir.is_dir() or self.usage_db.is_file():
            return True
        return _has_local_deile_process()

    @property
    def mode_label(self) -> str:
        """Rótulo curto para o header do painel."""
        if self.demo:
            return "demo (mocks)"
        k = self.k8s_available
        ll = self.local_available
        if k and ll:
            return "k8s + local"
        if k:
            return "k8s only"
        if ll:
            return "local only"
        return "vazio (sem fontes)"


def _has_local_deile_process() -> bool:
    """Detecta processo `python ... (deile|deilebot)` no host.

    Usa `ps -axo command=` (POSIX). Falha silenciosa se `ps` ausente — o
    callsite (`RuntimeContext.local_available`) cai pro fallback de
    "tem logs/DB?".
    """
    ps = shutil.which("ps")
    if ps is None:
        return False
    try:
        out = subprocess.run(
            [ps, "-axo", "command="],
            capture_output=True, text=True, timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if out.returncode != 0:
        return False
    for line in out.stdout.splitlines():
        if _LOCAL_PROCESS_RE.search(line):
            return True
    return False


# ===== cache ================================================================

@dataclass
class Cache(Generic[T]):
    """Cache TTL ao redor de um fetcher.

    Em caso de erro, mantém o último valor bom; se nunca houve valor bom,
    devolve o `fallback`. Guarda a última exceção em `last_error` para a
    view exibir um indicador discreto.

    Contrato de blocking:
    - ``get()`` **nunca bloqueia** depois do primeiro fetch — retorna o
      valor cacheado mesmo que esteja velho.
    - ``maybe_refresh()`` é o que o ``BackgroundRefresher`` chama em loop
      para manter o cache fresco (faz fetch só se TTL venceu).
    - ``get(force=True)`` continua síncrono — usado pelo hotkey [r] para
      cold-start no primeiro render.

    Antes desta separação, o render no thread principal eventualmente
    caía no ramo de fetch quando o TTL vencia, e qualquer subprocess
    kubectl/gh segurava a UI por segundos. Agora todo I/O pesado vive em
    ``BackgroundRefresher``.
    """

    ttl_s: float
    fetcher: Callable[[], T]
    fallback: T
    _value: Optional[T] = field(default=None, init=False, repr=False)
    _fetched_at: float = field(default=0.0, init=False, repr=False)
    _last_error: Optional[str] = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False,
    )

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def age_s(self) -> float:
        return time.monotonic() - self._fetched_at if self._fetched_at else 0.0

    def invalidate(self) -> None:
        """Força o próximo `get()` a refazer o fetch.

        Substitui o acesso direto a `_fetched_at = 0.0` (que tinha race
        entre threads e ignorava o lock interno).
        """
        with self._lock:
            self._fetched_at = 0.0

    def get(self, force: bool = False) -> T:
        """Devolve o valor cacheado, sem bloquear (a menos que `force`).

        - Há valor + ``force=False``: retorna instantâneo, **mesmo se
          velho**. O ``BackgroundRefresher`` cuida do refresh.
        - Sem valor (cold start): faz fetch síncrono uma vez, para a
          primeira render ter algo.
        - ``force=True``: faz fetch síncrono (usado pelos testes e por
          comportamento legado).
        """
        if self._value is not None and not force:
            return self._value
        return self._refresh()

    def maybe_refresh(self) -> bool:
        """Refaz o fetch se o TTL venceu. Para uso do BackgroundRefresher.

        Retorna ``True`` se realmente fez fetch novo.
        """
        if (self._value is not None
                and (time.monotonic() - self._fetched_at) < self.ttl_s):
            return False
        self._refresh()
        return True

    def _refresh(self) -> T:
        # Combinação dos dois lados do merge: lock thread-safe + logger
        # debug em falha (vindo do branch remoto a68ace9), mas com o
        # caminho "não bloqueia depois do primeiro fetch" do lado novo
        # (theirs / BackgroundRefresher). `invalidate()` já vive acima e
        # apenas zera `_fetched_at` — o `get()` passa a tratar isso como
        # "cold start" e refaz fetch único na próxima chamada.
        with self._lock:
            try:
                new = self.fetcher()
                self._value = new
                self._fetched_at = time.monotonic()
                self._last_error = None
                return new
            except Exception as exc:  # noqa: BLE001 (defensive — render must never crash)
                logger.debug(
                    "Cache.refresh fetcher failed: %s", exc, exc_info=True,
                )
                self._last_error = f"{type(exc).__name__}: {exc}"
                if self._value is None:
                    return self.fallback
                return self._value


# ===== subprocess helpers ===================================================

def _resolve(tool: str) -> Optional[str]:
    """Acha um binário no PATH ou no diretório do Rancher Desktop."""
    found = shutil.which(tool)
    if found:
        return found
    rd = Path.home() / ".rd" / "bin" / tool
    return str(rd) if rd.is_file() else None


def kubectl_bin() -> Optional[str]:
    return _resolve("kubectl")


def gh_bin() -> Optional[str]:
    return shutil.which("gh")


def _capture_json(cmd: List[str], timeout: float = 5.0) -> Optional[Any]:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def _capture_text(cmd: List[str], timeout: float = 5.0) -> Optional[str]:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


# ===== time helpers =========================================================

_UTC = timezone.utc


def _parse_k8s_ts(s: Optional[str]) -> Optional[datetime]:
    """Decoder leniente para timestamps do Kubernetes (RFC3339)."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+[+\-Z][\d:]*)\s+(.*)$")


def _parse_log_line(line: str) -> Optional["LogLine"]:
    """Extrai timestamp + corpo de uma linha de `kubectl logs --timestamps`."""
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    ts = _parse_k8s_ts(m.group(1))
    if ts is None:
        return None
    return LogLine(ts=ts, body=m.group(2))


class _KubectlProviderMixin:
    """Reusa o padrão `_kubectl + _resolve_kubectl()` entre os providers.

    Operador pode ter instalado kubectl depois do painel abrir — toda
    chamada de fetch tenta re-resolver se a referência local ainda é None.

    Provider pode ser desabilitado (`enabled=False`) para o caso
    `--local-only`: `_fetch` é interceptado por `_check_enabled`, que
    levanta `RuntimeError("k8s disabled")` antes do subprocess. O
    `Cache` captura e devolve o fallback (`[]`, `{}`, `PipelineState()`,
    etc) — UI vê os painéis vazios em vez de dados do cluster, sem
    chamar `kubectl` desnecessariamente.
    """

    _kubectl: Optional[str]
    _enabled: bool = True

    def _resolve_kubectl(self) -> Optional[str]:
        if self._kubectl is None:
            self._kubectl = kubectl_bin()
        return self._kubectl

    def _check_enabled(self) -> None:
        """Raise para o Cache cair em fallback quando provider desabilitado."""
        if not self._enabled:
            raise RuntimeError("k8s desabilitado (--local-only)")


def _fmt_age(seconds: Optional[float]) -> str:
    """Idade humanamente legível: 3s, 47s, 2m, 1h12m, 3d."""
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s % 3600 else f"{s // 3600}h"
    return f"{s // 86400}d"


# ===== Pods provider ========================================================

@dataclass
class PodInfo:
    name: str
    role: str            # 'pipeline' | 'worker' | 'bot' | 'shell' | 'other'
    status: str          # 'Running' | 'Pending' | etc
    ready: bool
    restarts: int
    age_s: float
    started_at: Optional[datetime]
    node: str = ""


_ROLE_BY_APP = {
    "deile-pipeline": "pipeline",
    "deile-worker":   "worker",
    "deilebot":       "bot",
    "deile-shell":    "shell",
}


class PodsProvider(_KubectlProviderMixin):
    """Lista os pods do namespace em forma tipada (default `deile`)."""

    def __init__(self, ttl_s: float = 1.0, namespace: str = NS,
                 enabled: bool = True):
        # 1s: `kubectl get pods` local <50ms, sem custo perceptível.
        self._kubectl = kubectl_bin()
        self._namespace = namespace
        self._enabled = enabled
        self._cache: Cache[List[PodInfo]] = Cache(ttl_s, self._fetch, fallback=[])

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> List[PodInfo]:
        return self._cache.get(force)

    def _fetch(self) -> List[PodInfo]:
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        data = _capture_json(
            [self._kubectl, "-n", self._namespace, "get", "pods", "-o", "json"],
            timeout=4.0,
        )
        if data is None:
            raise RuntimeError("kubectl get pods falhou")
        rows: List[PodInfo] = []
        now = datetime.now(_UTC)
        for item in data.get("items", []):
            meta = item.get("metadata", {})
            labels = meta.get("labels", {})
            app = labels.get("app", "")
            role = _ROLE_BY_APP.get(app, "other")
            status = item.get("status", {})
            phase = status.get("phase", "Unknown")
            container_statuses = status.get("containerStatuses", []) or []
            ready = all(cs.get("ready", False) for cs in container_statuses) \
                if container_statuses else False
            restarts = sum(cs.get("restartCount", 0) for cs in container_statuses)
            started_at = _parse_k8s_ts(status.get("startTime"))
            age_s = (now - started_at).total_seconds() if started_at else 0.0
            rows.append(PodInfo(
                name=meta.get("name", "?"),
                role=role,
                status=phase,
                ready=ready,
                restarts=restarts,
                age_s=age_s,
                started_at=started_at,
                node=item.get("spec", {}).get("nodeName", ""),
            ))
        # Ordem estável: pipeline > worker > bot > shell > outros, por nome.
        order = {"pipeline": 0, "worker": 1, "bot": 2, "shell": 3, "other": 4}
        rows.sort(key=lambda p: (order.get(p.role, 9), p.name))
        return rows


# ===== Log line + pipeline/worker activity =================================

@dataclass
class LogLine:
    ts: datetime
    body: str


@dataclass
class ActivityEvent:
    ts: datetime
    actor: str          # 'pipeline' | 'worker-<short>' | 'bot' | 'notifier'
    action: str         # 'dispatch' | 'mention' | 'http' | 'startup' | 'other'
    target: str         # '#296' | 'PR#291' | ''
    detail: str         # texto livre curto

    @property
    def hhmmss(self) -> str:
        return self.ts.astimezone().strftime("%H:%M:%S")


_DISPATCH_START_RE = re.compile(r"worker dispatch starting", re.IGNORECASE)
_DISPATCH_DONE_RE = re.compile(r"worker dispatch completed", re.IGNORECASE)
_HTTP_POST_RE = re.compile(
    r'HTTP Request: POST .*?/v1/dispatch.*?"HTTP/[\d.]+ (\d+)', re.IGNORECASE
)
_MENTION_RE = re.compile(
    r"mention group (issue|pr):(\d+): triggers=\[(.*?)\]", re.IGNORECASE
)
_STAGES_RE = re.compile(
    r"deile\.orchestration\.pipeline\.stages\s+(.*)$", re.IGNORECASE
)
_STARTUP_RE = re.compile(r"starting pipeline monitor", re.IGNORECASE)


def _classify_pipeline_line(ll: LogLine) -> Optional[ActivityEvent]:
    """Reduz uma linha bruta a um ActivityEvent semântico, se reconhecida."""
    body = ll.body
    m = _MENTION_RE.search(body)
    if m:
        kind = m.group(1).lower()
        num = m.group(2)
        triggers = m.group(3).replace("'", "").replace('"', "")
        target = f"{'PR' if kind == 'pr' else '#'}{num}"
        return ActivityEvent(
            ts=ll.ts, actor="pipeline", action="mention",
            target=target, detail=f"triggers={triggers}",
        )
    if _DISPATCH_START_RE.search(body):
        return ActivityEvent(
            ts=ll.ts, actor="pipeline", action="dispatch", target="",
            detail="worker dispatch starting",
        )
    if _DISPATCH_DONE_RE.search(body):
        return ActivityEvent(
            ts=ll.ts, actor="pipeline", action="dispatch", target="",
            detail="worker dispatch completed",
        )
    m = _HTTP_POST_RE.search(body)
    if m:
        return ActivityEvent(
            ts=ll.ts, actor="pipeline", action="http", target="",
            detail=f"POST /v1/dispatch → {m.group(1)}",
        )
    if _STARTUP_RE.search(body):
        return ActivityEvent(
            ts=ll.ts, actor="pipeline", action="startup", target="",
            detail="pipeline monitor started",
        )
    m = _STAGES_RE.search(body)
    if m:
        # Linhas do stages.py mais raras (claim/refine/block) caem aqui em forma genérica.
        return ActivityEvent(
            ts=ll.ts, actor="pipeline", action="stages",
            target="", detail=m.group(1).strip(),
        )
    return None


@dataclass
class PipelineState:
    """Estado deduzido do log do `deile-pipeline`."""
    running_since: Optional[datetime] = None
    last_action_ts: Optional[datetime] = None
    last_action_summary: str = "—"
    last_dispatch_ts: Optional[datetime] = None
    dispatches_24h: int = 0
    mentions_24h: int = 0
    events: List[ActivityEvent] = field(default_factory=list)
    raw_lines: int = 0

    @property
    def running_for_s(self) -> Optional[float]:
        if self.running_since is None:
            return None
        return (datetime.now(_UTC) - self.running_since).total_seconds()

    @property
    def last_action_age_s(self) -> Optional[float]:
        if self.last_action_ts is None:
            return None
        return (datetime.now(_UTC) - self.last_action_ts).total_seconds()


class PipelineProvider(_KubectlProviderMixin):
    """Lê os últimos N segundos de log do deile-pipeline e classifica."""

    DEPLOY = "deile-pipeline"  # default histórico; override via __init__
    TAIL_LINES = 200

    def __init__(self, ttl_s: float = 2.0,
                 namespace: str = NS, deploy: Optional[str] = None,
                 enabled: bool = True):
        # 2s: `kubectl logs --tail=200` ~200ms; balanceado entre vivo e custo.
        self._kubectl = kubectl_bin()
        self._namespace = namespace
        self._deploy = deploy or self.DEPLOY
        self._enabled = enabled
        self._cache: Cache[PipelineState] = Cache(
            ttl_s, self._fetch, fallback=PipelineState(),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> PipelineState:
        return self._cache.get(force)

    def _fetch(self) -> PipelineState:
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        text = _capture_text(
            [self._kubectl, "-n", self._namespace, "logs",
             f"deploy/{self._deploy}",
             f"--tail={self.TAIL_LINES}", "--timestamps"],
            timeout=5.0,
        )
        if text is None:
            raise RuntimeError(f"kubectl logs {self._deploy} falhou")
        return self._parse(text)

    def _parse(self, text: str) -> PipelineState:
        state = PipelineState()
        now = datetime.now(_UTC)
        cutoff_24h = now - timedelta(hours=24)
        # Cap defensivo: pods com saída multi-MB não devem virar strings de
        # MB em memória. Mantemos o final do log (mais recente).
        if len(text) > MAX_LOG_BYTES:
            text = text[-MAX_LOG_BYTES:]
        for raw in text.splitlines():
            state.raw_lines += 1
            ll = _parse_log_line(raw)
            if ll is None:
                continue
            ev = _classify_pipeline_line(ll)
            if ev is None:
                continue
            if ev.action == "startup":
                state.running_since = ev.ts
            if ev.action == "dispatch" and "starting" in ev.detail:
                state.last_dispatch_ts = ev.ts
                if ev.ts >= cutoff_24h:
                    state.dispatches_24h += 1
            if ev.action == "mention" and ev.ts >= cutoff_24h:
                state.mentions_24h += 1
            state.last_action_ts = ev.ts
            state.last_action_summary = self._summary(ev)
            state.events.append(ev)
        # Mantém só os 60 mais recentes — o feed da UI nunca precisa de mais.
        state.events = state.events[-60:]
        return state

    @staticmethod
    def _summary(ev: ActivityEvent) -> str:
        if ev.action == "mention":
            return f"mention {ev.target}: {ev.detail}"
        if ev.action == "dispatch":
            return ev.detail
        if ev.action == "http":
            return f"dispatch {ev.detail}"
        if ev.action == "stages":
            return ev.detail[:80]
        return ev.detail[:80]


# ===== Worker activity ======================================================

@dataclass
class WorkerState:
    """Estado deduzido do log de um pod worker."""
    pod_name: str
    busy: bool = False
    last_dispatch_ts: Optional[datetime] = None
    last_substantive_ts: Optional[datetime] = None
    last_health_ts: Optional[datetime] = None
    last_substantive_body: str = ""

    @property
    def last_activity_s(self) -> Optional[float]:
        ts = self.last_substantive_ts or self.last_dispatch_ts
        if ts is None:
            ts = self.last_health_ts
        if ts is None:
            return None
        return (datetime.now(_UTC) - ts).total_seconds()


_WORKER_HEALTH_RE = re.compile(r"GET /v1/health", re.IGNORECASE)
_WORKER_DISPATCH_RE = re.compile(r"POST /v1/dispatch", re.IGNORECASE)
_WORKER_BUSY_WINDOW_S = 90  # se houve POST /v1/dispatch nos últimos 90s, está busy


class WorkerProvider(_KubectlProviderMixin):
    """Por pod worker, deduz busy/idle do log."""

    LABEL_SELECTOR_FMT = "app={deploy}"
    TAIL_LINES = 200

    def __init__(self, ttl_s: float = 2.0,
                 namespace: str = NS, worker_deploy: str = "deile-worker",
                 enabled: bool = True):
        # 2s: N `kubectl logs` (1 por worker) — só roda em background.
        self._kubectl = kubectl_bin()
        self._namespace = namespace
        self._worker_deploy = worker_deploy
        self._enabled = enabled
        self._cache: Cache[Dict[str, WorkerState]] = Cache(
            ttl_s, self._fetch, fallback={},
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> Dict[str, WorkerState]:
        return self._cache.get(force)

    def _fetch(self) -> Dict[str, WorkerState]:
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        selector = self.LABEL_SELECTOR_FMT.format(deploy=self._worker_deploy)
        names_raw = _capture_text(
            [self._kubectl, "-n", self._namespace, "get", "pods",
             "-l", selector,
             "-o", "jsonpath={.items[*].metadata.name}"],
            timeout=4.0,
        )
        if names_raw is None:
            raise RuntimeError("kubectl get worker pods falhou")
        pod_names = [n for n in names_raw.split() if n]
        states: Dict[str, WorkerState] = {}
        for name in pod_names:
            text = _capture_text(
                [self._kubectl, "-n", self._namespace, "logs", name,
                 f"--tail={self.TAIL_LINES}", "--timestamps"],
                timeout=4.0,
            ) or ""
            states[name] = self._parse(name, text)
        return states

    def _parse(self, pod_name: str, text: str) -> WorkerState:
        state = WorkerState(pod_name=pod_name)
        now = datetime.now(_UTC)
        # Cap defensivo contra logs muito grandes.
        if len(text) > MAX_LOG_BYTES:
            text = text[-MAX_LOG_BYTES:]
        for raw in text.splitlines():
            ll = _parse_log_line(raw)
            if ll is None:
                continue
            if _WORKER_HEALTH_RE.search(ll.body):
                state.last_health_ts = ll.ts
                continue
            if _WORKER_DISPATCH_RE.search(ll.body):
                # `last_dispatch_ts` é a fonte de verdade do busy-window —
                # restrita a "POST /v1/dispatch" para casar com a constante
                # `_WORKER_BUSY_WINDOW_S` ("nos últimos 90s teve dispatch").
                state.last_dispatch_ts = ll.ts
                state.last_substantive_ts = ll.ts
                state.last_substantive_body = ll.body[:100]
                continue
            # Qualquer outra linha "não-health" conta como atividade real
            # (substantiva) mas NÃO sobe busy — só dispatch real faz isso.
            state.last_substantive_body = ll.body[:100]
            if (state.last_substantive_ts is None
                    or ll.ts > state.last_substantive_ts):
                state.last_substantive_ts = ll.ts
        if state.last_dispatch_ts is not None:
            since = (now - state.last_dispatch_ts).total_seconds()
            state.busy = since < _WORKER_BUSY_WINDOW_S
        return state


# ===== GitHub provider ======================================================

@dataclass
class GitHubIssue:
    number: int
    title: str
    is_pr: bool
    state: str           # 'open' | 'closed'
    labels: List[str]
    assignees: List[str]
    updated_at: Optional[datetime]
    url: str
    # Estado derivado das labels do pipeline
    workflow: str = ""   # ex: 'em_implementacao'
    review: str = ""     # ex: 'pendente'
    blocked: bool = False
    refining: bool = False


@dataclass
class GitHubSnapshot:
    issues: List[GitHubIssue] = field(default_factory=list)
    prs: List[GitHubIssue] = field(default_factory=list)

    @cached_property
    def issue_states(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for it in self.issues:
            key = it.workflow or "sem_workflow"
            counts[key] = counts.get(key, 0) + 1
        return counts

    @cached_property
    def pr_states(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for pr in self.prs:
            key = pr.review or "sem_review"
            counts[key] = counts.get(key, 0) + 1
        return counts


_WORKFLOW_PREFIX = "~workflow:"
_REVIEW_PREFIX = "~review:"
_BLOCKED_LABEL = "~workflow:bloqueada"


def _derive_workflow(labels: List[str]) -> str:
    """Estado workflow efetivo. `bloqueada` vence — é terminal e o pipeline
    a respeita mesmo quando outra label de fase ainda está presente."""
    workflow_labels = [lbl[len(_WORKFLOW_PREFIX):]
                       for lbl in labels if lbl.startswith(_WORKFLOW_PREFIX)]
    if not workflow_labels:
        return ""
    if "bloqueada" in workflow_labels:
        return "bloqueada"
    return workflow_labels[0]


def _derive_review(labels: List[str]) -> str:
    for lbl in labels:
        if lbl.startswith(_REVIEW_PREFIX):
            return lbl[len(_REVIEW_PREFIX):]
    return ""


class GitHubProvider:
    """Lista issues e PRs abertos via `gh api`."""

    PER_PAGE = 100

    def __init__(self, repo: str = REPO_DEFAULT, ttl_s: float = 10.0):
        # 10s: `gh api --paginate` custa rate-limit (5000/h auth = 1.4/s);
        # 10s = 360/h, folga confortável.
        self._gh = gh_bin()
        self._repo = repo
        self._cache: Cache[GitHubSnapshot] = Cache(
            ttl_s, self._fetch, fallback=GitHubSnapshot(),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> GitHubSnapshot:
        return self._cache.get(force)

    def _fetch(self) -> GitHubSnapshot:
        if self._gh is None:
            raise RuntimeError("gh não encontrado")
        # /issues retorna issues + PRs no mesmo array; PR tem chave 'pull_request'.
        # `--paginate` percorre tudo; o único limite é o timeout de 15s
        # abaixo — repositórios com milhares de issues abertas atingem o
        # timeout antes do limite de páginas. Adicionar `--slurp --jq`
        # com cap explícito é evolução futura caso o teto fique pequeno.
        data = _capture_json(
            [self._gh, "api", "--paginate",
             f"/repos/{self._repo}/issues?state=open&per_page={self.PER_PAGE}"],
            timeout=15.0,
        )
        if data is None:
            raise RuntimeError("gh api issues falhou")
        snap = GitHubSnapshot()
        # `--paginate` pode retornar lista única ou várias páginas concatenadas
        # (lista de listas) dependendo do gh — normaliza.
        items: List[Dict[str, Any]] = []
        if isinstance(data, list):
            for chunk in data:
                if isinstance(chunk, list):
                    items.extend(chunk)
                else:
                    items.append(chunk)
        for it in items:
            labels = [lbl.get("name", "") for lbl in it.get("labels", []) or []]
            assignees = [a.get("login", "")
                         for a in it.get("assignees", []) or []]
            obj = GitHubIssue(
                number=int(it.get("number", 0)),
                title=it.get("title", ""),
                is_pr=("pull_request" in it),
                state=it.get("state", "open"),
                labels=labels,
                assignees=assignees,
                updated_at=_parse_k8s_ts(it.get("updated_at")),
                url=it.get("html_url", ""),
                workflow=_derive_workflow(labels),
                review=_derive_review(labels),
                blocked=_BLOCKED_LABEL in labels,
                refining=any(lbl == "refinar" for lbl in labels),
            )
            if obj.is_pr:
                snap.prs.append(obj)
            else:
                snap.issues.append(obj)
        snap.issues.sort(key=lambda x: x.number, reverse=True)
        snap.prs.sort(key=lambda x: x.number, reverse=True)
        return snap


# ===== Costs (UsageRepository) ==============================================

@dataclass
class CostsSnapshot:
    by_provider_24h: Dict[str, float] = field(default_factory=dict)
    by_provider_1h: Dict[str, float] = field(default_factory=dict)
    total_24h: float = 0.0
    total_1h: float = 0.0
    records_24h: int = 0
    top_sessions_24h: List[tuple] = field(default_factory=list)  # (session_id, cost)


class CostsProvider:
    """Agrega custos do `~/.deile/db/usage.db`.

    Fallback gracioso quando o DB ainda não existe (DEILE nunca rodou
    localmente nem montou o PVC do worker no host).
    """

    def __init__(self, db_path: Path = USAGE_DB, ttl_s: float = 30.0):
        # 30s: SQLite local; mais frequente é só polling barulhento.
        self._db_path = db_path
        self._cache: Cache[CostsSnapshot] = Cache(
            ttl_s, self._fetch, fallback=CostsSnapshot(),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> CostsSnapshot:
        return self._cache.get(force)

    def _fetch(self) -> CostsSnapshot:
        if not self._db_path.is_file():
            return CostsSnapshot()
        snap = CostsSnapshot()
        now = time.time()
        since_24h = now - 86400
        since_1h = now - 3600
        try:
            conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True, timeout=2.0,
            )
        except sqlite3.OperationalError:
            return snap
        try:
            cur = conn.cursor()
            for prov, cost in cur.execute(
                "SELECT provider_id, COALESCE(SUM(cost_usd),0) "
                "FROM usage_records WHERE timestamp>=? GROUP BY provider_id",
                (since_24h,),
            ):
                snap.by_provider_24h[prov] = float(cost)
                snap.total_24h += float(cost)
            for prov, cost in cur.execute(
                "SELECT provider_id, COALESCE(SUM(cost_usd),0) "
                "FROM usage_records WHERE timestamp>=? GROUP BY provider_id",
                (since_1h,),
            ):
                snap.by_provider_1h[prov] = float(cost)
                snap.total_1h += float(cost)
            count_row = cur.execute(
                "SELECT COUNT(*) FROM usage_records WHERE timestamp>=?",
                (since_24h,),
            ).fetchone()
            snap.records_24h = int(count_row[0]) if count_row else 0
            snap.top_sessions_24h = [
                (sid, float(cost))
                for sid, cost in cur.execute(
                    "SELECT session_id, SUM(cost_usd) AS c "
                    "FROM usage_records WHERE timestamp>=? "
                    "GROUP BY session_id ORDER BY c DESC LIMIT 5",
                    (since_24h,),
                )
            ]
        finally:
            conn.close()
        return snap


# ===== Model providers ======================================================

@dataclass
class ModelInfo:
    provider_id: str
    model_id: str
    display_name: str
    tier: str
    label: str
    input_cost_per_1m: float
    output_cost_per_1m: float

    @property
    def slug(self) -> str:
        return f"{self.provider_id}:{self.model_id}"


_MODELS_YAML_DEFAULT = (
    Path(__file__).resolve().parent.parent.parent
    / "deile" / "config" / "model_providers.yaml"
)


class ModelsProvider:
    """Catálogo de modelos disponíveis lido do `model_providers.yaml`.

    Lê uma vez (TTL 5min) — o catálogo é estático na vasta maioria do
    tempo; refresh é só pra cobrir o caso de edição em vivo do YAML.
    """

    def __init__(self, yaml_path: Path = _MODELS_YAML_DEFAULT,
                 ttl_s: float = 300.0):
        self._path = yaml_path
        self._cache: Cache[List[ModelInfo]] = Cache(
            ttl_s, self._fetch, fallback=[],
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> List[ModelInfo]:
        return self._cache.get(force)

    def _fetch(self) -> List[ModelInfo]:
        try:
            import yaml as _yaml  # lazy
        except ImportError as exc:
            raise RuntimeError(f"PyYAML ausente: {exc}") from exc
        if not self._path.is_file():
            raise RuntimeError(f"model_providers.yaml não encontrado em {self._path}")
        data = _yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        models = data.get("models") or []
        out: List[ModelInfo] = []
        for m in models:
            p = (m.get("pricing") or {})
            out.append(ModelInfo(
                provider_id=str(m.get("provider_id", "?")),
                model_id=str(m.get("model_id", "?")),
                display_name=str(m.get("display_name", m.get("model_id", "?"))),
                tier=str(m.get("tier", "—")),
                label=str(m.get("label", "—")),
                input_cost_per_1m=float(p.get("input_per_1m_usd", 0.0)),
                output_cost_per_1m=float(p.get("output_per_1m_usd", 0.0)),
            ))
        return out


class CurrentModelProvider(_KubectlProviderMixin):
    """Lê o `DEILE_PREFERRED_MODEL` setado no Deployment do worker (e/ou
    pipeline) — o valor que efetivamente roda agora nos pods."""

    DEFAULT_DEPLOYMENTS = ("deile-worker", "deile-pipeline")

    def __init__(self, deployments=DEFAULT_DEPLOYMENTS, ttl_s: float = 3.0,
                 namespace: str = NS, enabled: bool = True):
        # 3s: 1 `kubectl get -o json` por deployment (~100ms cada).
        self._kubectl = kubectl_bin()
        self._namespace = namespace
        self._deployments = tuple(deployments)
        self._enabled = enabled
        self._cache: Cache[Dict[str, Optional[str]]] = Cache(
            ttl_s, self._fetch, fallback={d: None for d in deployments},
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def deployments(self) -> tuple:
        return self._deployments

    def get(self, force: bool = False) -> Dict[str, Optional[str]]:
        return self._cache.get(force)

    def _fetch(self) -> Dict[str, Optional[str]]:
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        out: Dict[str, Optional[str]] = {}
        for dep in self._deployments:
            data = _capture_json(
                [self._kubectl, "-n", self._namespace, "get",
                 f"deployment/{dep}", "-o", "json"],
                timeout=4.0,
            )
            out[dep] = self._extract(data) if data else None
        return out

    @staticmethod
    def _extract(data: Dict[str, Any]) -> Optional[str]:
        containers = (data.get("spec", {}).get("template", {})
                      .get("spec", {}).get("containers", []) or [])
        if not containers:
            return None
        for env in (containers[0].get("env") or []):
            if env.get("name") == "DEILE_PREFERRED_MODEL":
                return env.get("value")
        return None


def _audit_security_policy_change(
    deployment: str, slug: str, *, result: str, detail: str,
    namespace: str = NS,
) -> None:
    """Emite AuditEvent(SECURITY_POLICY_CHANGED) para a troca de modelo.

    Silencioso se o pacote deile não estiver importável (e.g., rodando
    o painel em isolamento sem o módulo principal no sys.path). A falha
    de log NUNCA bloqueia a ação — mas a falha de validação sim.
    """
    try:
        from deile.security.audit_logger import (  # noqa: PLC0415
            AuditEventType, SeverityLevel, get_audit_logger)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit logger indisponível para set_preferred_model: %s", exc,
        )
        return
    # INFO para fluxos não-anômalos (allowed/completed/cancelled); WARNING
    # para falhas ou recusas — evita que toda troca bem-sucedida (ou um
    # cancelamento legítimo do operador) vire WARNING no log.
    severity = (SeverityLevel.INFO
                if result in ("allowed", "completed", "cancelled")
                else SeverityLevel.WARNING)
    try:
        get_audit_logger().log_event(
            event_type=AuditEventType.SECURITY_POLICY_CHANGED,
            severity=severity,
            actor="panel:set_preferred_model",
            resource=f"deployment:{namespace}/{deployment}:DEILE_PREFERRED_MODEL",
            action="kubectl_set_env",
            result=result,
            details={"slug": slug, "detail": detail[:200]},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("falha emitindo AuditEvent: %s", exc)


def set_preferred_model(deployment: str, slug: str,
                        timeout: float = 15.0,
                        namespace: str = NS) -> tuple:
    """Aplica `DEILE_PREFERRED_MODEL=<slug>` no Deployment.

    `kubectl set env` modifica a spec do Deployment, o que dispara
    rollout automático (com a strategy do manifest — `RollingUpdate`
    para o worker e `Recreate` para o pipeline). Retorna `(ok, msg)`.

    Slug é validado contra `_MODEL_SLUG_RE` antes de virar argv — rejeita
    quebras de linha, `=`, NUL, controle e espaços que permitiriam
    injeção em argumentos do kubectl. Toda chamada (allowed/denied/falha)
    emite `AuditEvent(SECURITY_POLICY_CHANGED)`.
    """
    # Normaliza valores não-string pra um repr seguro antes de logar — evita
    # crashes no audit quando o caller passa algo bizarro (None, dict, etc).
    safe_dep = deployment if isinstance(deployment, str) else repr(deployment)
    safe_slug = slug if isinstance(slug, str) else repr(slug)
    if deployment not in _ALLOWED_DEPLOYMENTS:
        _audit_security_policy_change(
            safe_dep, safe_slug,
            result="denied", detail="deployment fora da whitelist",
            namespace=namespace,
        )
        allowed = ", ".join(sorted(_ALLOWED_DEPLOYMENTS))
        return False, (
            f"deployment '{deployment}' não permitido — "
            f"esperado um de: {allowed}"
        )
    if not isinstance(slug, str) or not _MODEL_SLUG_RE.match(slug):
        _audit_security_policy_change(
            deployment, safe_slug,
            result="denied", detail="slug inválido",
            namespace=namespace,
        )
        return False, "slug inválido — recusado por validação"
    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_security_policy_change(
            deployment, slug, result="failed", detail="kubectl não encontrado",
            namespace=namespace,
        )
        return False, "kubectl não encontrado"
    # Audit ANTES de executar — registra a intenção, ainda que o
    # subprocess depois falhe ou trave.
    _audit_security_policy_change(
        deployment, slug, result="allowed", detail="executando kubectl set env",
        namespace=namespace,
    )
    try:
        proc = subprocess.run(
            [kubectl, "-n", namespace, "set", "env", f"deploy/{deployment}",
             f"DEILE_PREFERRED_MODEL={slug}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_security_policy_change(
            deployment, slug, result="failed", detail=f"subprocess: {exc}",
            namespace=namespace,
        )
        return False, f"falha ao executar kubectl: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        _audit_security_policy_change(
            deployment, slug, result="failed",
            detail=f"rc={proc.returncode} {err}",
            namespace=namespace,
        )
        return False, err
    msg = (proc.stdout or "rollout disparado").strip()
    _audit_security_policy_change(
        deployment, slug, result="completed", detail=msg,
        namespace=namespace,
    )
    return True, msg


# ===== Notifier (bot audit log) =============================================

class NotifierProvider(_KubectlProviderMixin):
    """Tail dos audit events do `deilebot` via kubectl logs.

    Sai como `List[str]` (linhas cruas) — a view filtra/parsea. Cache TTL
    5s evita o `kubectl logs deploy/deilebot --tail=500` por render (a
    NotifierEchoView refresha em 5s; era 12 chamadas/min sem cache).
    """

    BOT_DEPLOY = "deilebot"  # default; override via __init__
    TAIL = 500

    def __init__(self, ttl_s: float = 5.0,
                 namespace: str = NS, deploy: Optional[str] = None,
                 enabled: bool = True):
        self._kubectl = kubectl_bin()
        self._namespace = namespace
        self._deploy = deploy or self.BOT_DEPLOY
        self._enabled = enabled
        self._cache: Cache[List[str]] = Cache(ttl_s, self._fetch, fallback=[])

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> List[str]:
        return self._cache.get(force)

    def _fetch(self) -> List[str]:
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        text = _capture_text(
            [self._kubectl, "-n", self._namespace, "logs",
             f"deploy/{self._deploy}", f"--tail={self.TAIL}"],
            timeout=5.0,
        )
        if text is None:
            raise RuntimeError(f"kubectl logs {self._deploy} falhou")
        if len(text) > MAX_LOG_BYTES:
            text = text[-MAX_LOG_BYTES:]
        return text.splitlines()


# ===== Local mode providers =================================================
#
# Esses providers cobrem o caso "DEILE rodando direto no host (sem k8s)".
# Inspecionam:
#   - `ps` para detectar processos `python ... (deile|deilebot)`
#   - tail-from-end de `~/.deile/logs/deile.log` (até 64KB)
#   - tail-from-end de `~/.deile/logs/security_audit.log` (JSONL)
#
# Convenção: cada provider falha gracioso quando a fonte está vazia (sem
# `ps`, sem arquivo, sem permissão). O `last_error` fica preenchido para
# a UI mostrar como aviso no canto, mas o painel continua respondendo.

@dataclass
class LocalProcessInfo:
    pid: int
    role: str        # 'local-deile' | 'local-pipeline' | 'local-bot' | 'local-other'
    cmd: str         # cmdline truncado
    cpu_pct: float
    rss_kb: int
    etime_s: int     # uptime em segundos

    @property
    def name(self) -> str:
        """Nome estável para a UI — usado como chave de seleção."""
        return f"{self.role}#{self.pid}"

    @property
    def age_human(self) -> str:
        return _fmt_age(float(self.etime_s))

    @property
    def rss_human(self) -> str:
        """RSS em MB legível (KB → MB)."""
        return f"{self.rss_kb / 1024:.0f}MB" if self.rss_kb else "—"


def _parse_etime(s: str) -> int:
    """Converte `ps -o etime`: `[[DD-]HH:]MM:SS` → segundos.

    Formatos válidos: `01:23`, `12:34:56`, `2-03:04:05`. Aceita lixo
    devolvendo 0 — pra não derrubar o parser do _fetch.
    """
    if "-" in s:
        d, rest = s.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return 0
        s = rest
    else:
        days = 0
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = (int(parts[0]), int(parts[1]), int(parts[2]))
        elif len(parts) == 2:
            h, m, sec = (0, int(parts[0]), int(parts[1]))
        else:
            return 0
    except ValueError:
        return 0
    return days * 86400 + h * 3600 + m * 60 + sec


def _classify_local_process(cmd: str) -> Optional[str]:
    """Devolve role (`local-*`) ou None se a linha não é DEILE-like."""
    for pat, role in _LOCAL_PROCESS_PATTERNS:
        if pat.search(cmd):
            return role
    if _LOCAL_PROCESS_RE.search(cmd):
        return "local-other"
    return None


class LocalProcessesProvider:
    """Detecta processos DEILE/deilebot rodando no host.

    Usa `ps -axo pid,pcpu,rss,etime,command` (POSIX) — sem dependência
    externa (psutil opcional não-usado). Cacheia com TTL 2s para alinhar
    com `PodsProvider` (mesma frequência de refresh visual).

    Falha gracioso: `ps` ausente, comando devolve erro, ou nenhum
    processo casa → lista vazia + `last_error` preenchido.
    """

    def __init__(self, ttl_s: float = 2.0):
        self._cache: Cache[List[LocalProcessInfo]] = Cache(
            ttl_s, self._fetch, fallback=[],
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> List[LocalProcessInfo]:
        return self._cache.get(force)

    def _fetch(self) -> List[LocalProcessInfo]:
        ps = shutil.which("ps")
        if ps is None:
            raise RuntimeError("ps não encontrado")
        try:
            out = subprocess.run(
                [ps, "-axo", "pid=,pcpu=,rss=,etime=,command="],
                capture_output=True, text=True, timeout=4.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"ps falhou: {exc}") from exc
        if out.returncode != 0:
            raise RuntimeError(f"ps retornou rc={out.returncode}")
        results: List[LocalProcessInfo] = []
        my_pid = os.getpid()
        for line in out.stdout.splitlines():
            info = self._parse_line(line)
            if info is None:
                continue
            # Filtra o próprio processo do painel — `python ... infra/k8s/deploy.py
            # k8s panel` casa o padrão genérico `deile` e poluiria a lista.
            if info.pid == my_pid:
                continue
            results.append(info)
        # Ordem: deile > pipeline > bot > other; depois pid asc.
        order = {"local-deile": 0, "local-pipeline": 1,
                 "local-bot": 2, "local-other": 3}
        results.sort(key=lambda p: (order.get(p.role, 9), p.pid))
        return results

    @staticmethod
    def _parse_line(line: str) -> Optional[LocalProcessInfo]:
        # 4 first tokens are fixed-width, command is the remainder.
        parts = line.split(None, 4)
        if len(parts) < 5:
            return None
        pid_s, cpu_s, rss_s, etime_s, cmd = parts
        role = _classify_local_process(cmd)
        if role is None:
            return None
        try:
            pid = int(pid_s)
        except ValueError:
            return None
        try:
            cpu_pct = float(cpu_s)
        except ValueError:
            cpu_pct = 0.0
        try:
            rss_kb = int(rss_s)
        except ValueError:
            rss_kb = 0
        return LocalProcessInfo(
            pid=pid, role=role, cmd=cmd.strip(),
            cpu_pct=cpu_pct, rss_kb=rss_kb,
            etime_s=_parse_etime(etime_s),
        )


# ----- Local logs (deile.log) -----

_LOG_TAIL_BYTES = 64 * 1024  # 64KB ≈ 400-500 linhas; suficiente para feed vivo

# Formato canônico do logging.FileHandler do DEILE:
# `2026-05-23 19:39:01,234 - module.name - LEVEL - message`
_DEILE_LOG_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[,.]?\d*)"
    r"\s*-?\s*(.*)$",
)


def _parse_local_log_line(line: str) -> Optional[LogLine]:
    """Decoder leniente para `~/.deile/logs/deile.log` (Python logging)."""
    m = _DEILE_LOG_TS_RE.match(line)
    if not m:
        return None
    ts_str = m.group(1).replace(",", ".").replace(" ", "T")
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return None
    if ts.tzinfo is None:
        # `deile.log` usa hora local — assume timezone do sistema.
        local_tz = datetime.now().astimezone().tzinfo
        ts = ts.replace(tzinfo=local_tz)
    return LogLine(ts=ts, body=m.group(2))


@dataclass
class LocalLogsState:
    """Equivalente ao PipelineState para o `~/.deile/logs/deile.log`."""
    log_path: str = ""
    last_action_ts: Optional[datetime] = None
    last_action_summary: str = "—"
    events: List[ActivityEvent] = field(default_factory=list)
    raw_lines: int = 0
    file_size_kb: int = 0

    @property
    def last_action_age_s(self) -> Optional[float]:
        if self.last_action_ts is None:
            return None
        return (datetime.now(_UTC) - self.last_action_ts).total_seconds()


def _tail_file_bytes(path: Path, n_bytes: int) -> str:
    """Lê os últimos `n_bytes` de `path` (UTF-8 leniente).

    Descarta a primeira linha potencialmente incompleta se o offset > 0.
    Devolve string vazia se o arquivo está vazio ou inacessível.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size == 0:
        return ""
    offset = max(0, size - n_bytes)
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            blob = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    if offset > 0 and "\n" in blob:
        blob = blob.split("\n", 1)[1]
    return blob


class LocalLogsProvider:
    """Tail leniente de `~/.deile/logs/deile.log` + classify.

    Lê só os últimos `_LOG_TAIL_BYTES` (~64KB) — não importa quão
    grande o arquivo seja (já vi 25MB em produção). Cache TTL 2s para
    UI parecer viva sem martelar disco.
    """

    def __init__(self, log_path: Path, ttl_s: float = 2.0):
        self._log_path = log_path
        self._cache: Cache[LocalLogsState] = Cache(
            ttl_s, self._fetch, fallback=LocalLogsState(log_path=str(log_path)),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> LocalLogsState:
        return self._cache.get(force)

    def _fetch(self) -> LocalLogsState:
        state = LocalLogsState(log_path=str(self._log_path))
        if not self._log_path.is_file():
            return state
        try:
            state.file_size_kb = int(self._log_path.stat().st_size / 1024)
        except OSError:
            state.file_size_kb = 0
        blob = _tail_file_bytes(self._log_path, _LOG_TAIL_BYTES)
        if not blob:
            return state
        for raw in blob.splitlines():
            state.raw_lines += 1
            ll = _parse_local_log_line(raw)
            if ll is None:
                continue
            # Reusa o classificador do pipeline — os bodies têm o mesmo
            # formato (mesmo logger do DEILE, com ou sem prefixo kubectl).
            ev = _classify_pipeline_line(ll)
            if ev is None:
                continue
            state.last_action_ts = ll.ts
            state.last_action_summary = ev.detail[:80]
            ev_local = ActivityEvent(
                ts=ll.ts, actor="local", action=ev.action,
                target=ev.target, detail=ev.detail,
            )
            state.events.append(ev_local)
        state.events = state.events[-60:]
        return state


# ----- Local audit (security_audit.log) -----

class LocalAuditProvider:
    """Tail do `~/.deile/logs/security_audit.log` (uma linha = um AuditEvent JSON).

    Parser tenta JSON puro (formato canônico do AuditLogger); cai pro
    extract `{...}` inline se o arquivo for misturado com linhas
    prefixadas pelo runtime. Retorna eventos já parseados como dicts.
    """

    def __init__(self, audit_path: Path, ttl_s: float = 3.0,
                 tail_kb: int = 64):
        self._audit_path = audit_path
        self._tail_bytes = tail_kb * 1024
        self._cache: Cache[List[Dict[str, Any]]] = Cache(
            ttl_s, self._fetch, fallback=[],
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> List[Dict[str, Any]]:
        return self._cache.get(force)

    def _fetch(self) -> List[Dict[str, Any]]:
        if not self._audit_path.is_file():
            return []
        blob = _tail_file_bytes(self._audit_path, self._tail_bytes)
        if not blob:
            return []
        events: List[Dict[str, Any]] = []
        for line in blob.splitlines():
            line = line.strip()
            if not line:
                continue
            ev = _parse_audit_line(line)
            if ev is not None:
                events.append(ev)
        # Mantém só os 100 mais recentes; suficiente pra UI rolar.
        return events[-100:]


def _parse_audit_line(line: str) -> Optional[Dict[str, Any]]:
    """Tenta JSON puro; fallback: extrai o `{...}` inline."""
    if line.startswith("{"):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    brace = line.find("{")
    if brace < 0:
        return None
    try:
        obj = json.loads(line[brace:])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# ----- Local instances (state files publicados por processo) ----------------
#
# Cada instância DEILE rodando no host publica um state file JSON em
# `<runtime_dir>/<instance_id>.json` (Agent A, issue #303). Esse provider
# lê esses arquivos e expõe um snapshot por PID — assim o painel mostra
# `current_action` por processo (em vez do summary global do log, que era
# o mesmo texto pra todos os PIDs — o bug que esta feature resolve).
#
# Contrato com Agent A (schema_version=1): cada arquivo contém pelo menos
# `pid`, `instance_id`, `role`, `started_at`, `last_heartbeat_at`,
# `current_action` (ou null), `stats`. Mudanças de schema bumpam
# `schema_version` — versões desconhecidas são puladas (forward compat).
#
# GC: arquivos órfãos (PID já não existe) aparecem em crashes. O provider
# faz `unlink` silencioso no `_fetch` — evita lista poluída e mantém o
# diretório limpo sem precisar de cron externo.

# Schema atual suportado. Aumentar quando o contrato com Agent A mudar
# de forma incompatível (campo renomeado/removido).
_INSTANCE_SCHEMA_VERSION = 1
# Limite default para considerar um state file "stale": last_heartbeat
# muito velho sugere que o processo morreu sem cleanup ou o publisher
# travou. UI pode dim a linha; provider mantém na lista.
_INSTANCE_STALE_AFTER_S = 30.0
# Cap para o tamanho de detail/model — evita render-cost surpresa por
# campos inflados, e mantém doing_now_label curto na tabela.
_INSTANCE_DETAIL_MAX = 48
_INSTANCE_MODEL_MAX = 48


def _pid_alive(pid: int) -> bool:
    """`True` se o PID está vivo (UNIX).

    Cópia local da função canônica em `deile/runtime/instance_state.py`
    (Agent A, issue #303) — duplicada **intencionalmente** para manter
    `infra/k8s/_panel*.py` rodável sem o pacote `deile` instalado
    (mesma convenção do `audit_logger` import com fallback). Se Agent A
    refatorar a semântica de pid_alive, sincronizar aqui.

    Implementação: `os.kill(pid, 0)` — sinal 0 só checa permissão de
    enviar, não envia nada. ProcessLookupError = morto; PermissionError
    = vivo mas inacessível (assume vivo, pra não derrubar uma instância
    real que rodou sob outro usuário).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID existe mas não temos permissão de sinalizar — alguém de outro
        # uid rodando DEILE no mesmo host. Vivo até prova em contrário.
        return True
    except OSError:
        return False


def _parse_iso_ts(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO8601 com `+00:00` ou `Z` (formato escrito por Agent A)."""
    if not s or not isinstance(s, str):
        return None
    s2 = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s2)
    except ValueError:
        return None


@dataclass
class InstanceSnapshot:
    """Snapshot tipado de um state file `<runtime_dir>/<id>.json`.

    Expõe somente os campos consumidos pela UI — campos extras do JSON
    são silenciosamente ignorados (forward compat com Agent A adicionando
    metadados).

    Texto de `current_action` é truncado em `_INSTANCE_DETAIL_MAX` chars
    no parse — manter a tabela "doing now" sempre curta sem trabalho de
    render. `doing_now_label` é texto puro (sem emoji) por convenção do
    projeto: o painel evita emojis salvo opt-in explícito.
    """

    instance_id: str
    pid: int
    role: str
    started_at: Optional[datetime]
    last_heartbeat_at: Optional[datetime]
    current_action_kind: str          # 'idle' quando current_action is None
    current_action_detail: str        # '' quando current_action is None
    current_action_started_at: Optional[datetime]
    current_action_model: str         # '' quando faltar
    stats_tokens_in: int
    stats_tokens_out: int
    stats_cost_usd: float
    stats_turns: int
    stats_tool_calls: int
    stats_errors: int
    stale: bool                       # True quando last_heartbeat > stale_after_s

    @property
    def doing_now_label(self) -> str:
        """Texto curto pra coluna 'doing now' da tabela LOCAL PROCESSES.

        Mapeamento (sem emoji):
        - `idle`             → "idle"
        - `starting`         → "starting…"
        - `shutting_down`    → "shutting down"
        - `tool_execution`   → "tool: <detail>"
        - `llm_call`         → "llm: <model-or-detail>"
        - desconhecido       → "<kind>: <detail>" (forward-compat)
        """
        kind = self.current_action_kind or "idle"
        detail = self.current_action_detail or ""
        if kind == "idle":
            return "idle"
        if kind == "starting":
            return "starting…"
        if kind == "shutting_down":
            return "shutting down"
        if kind == "tool_execution":
            return f"tool: {detail}" if detail else "tool"
        if kind == "llm_call":
            label = self.current_action_model or detail
            return f"llm: {label}" if label else "llm"
        # Forward-compat: kind desconhecida vira `<kind>: <detail>` cru.
        return f"{kind}: {detail}".rstrip(": ").strip()


def _snapshot_from_payload(
    payload: Dict[str, Any], *, now: datetime, stale_after_s: float,
) -> Optional[InstanceSnapshot]:
    """Constrói `InstanceSnapshot` a partir do dict carregado do JSON.

    Retorna None se o payload não casa o `schema_version` esperado ou
    falta o `pid` (mínimo absoluto para indexar). Demais campos têm
    default sensato — Agent A pode escrever `stats` parcial sem quebrar.
    """
    try:
        sv = int(payload.get("schema_version", 0))
    except (TypeError, ValueError):
        sv = 0
    if sv != _INSTANCE_SCHEMA_VERSION:
        return None
    pid_raw = payload.get("pid")
    try:
        pid = int(pid_raw) if pid_raw is not None else 0
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    last_hb = _parse_iso_ts(payload.get("last_heartbeat_at"))
    stale = False
    if last_hb is not None:
        age = (now - last_hb).total_seconds()
        stale = age > stale_after_s
    action = payload.get("current_action") or {}
    if not isinstance(action, dict):
        action = {}
    kind = str(action.get("kind") or "idle")
    detail = str(action.get("detail") or "")[:_INSTANCE_DETAIL_MAX]
    model = str(action.get("model") or "")[:_INSTANCE_MODEL_MAX]
    action_started = _parse_iso_ts(action.get("started_at"))
    stats = payload.get("stats") or {}
    if not isinstance(stats, dict):
        stats = {}
    return InstanceSnapshot(
        instance_id=str(payload.get("instance_id") or f"pid-{pid}"),
        pid=pid,
        role=str(payload.get("role") or ""),
        started_at=_parse_iso_ts(payload.get("started_at")),
        last_heartbeat_at=last_hb,
        current_action_kind=kind,
        current_action_detail=detail,
        current_action_started_at=action_started,
        current_action_model=model,
        stats_tokens_in=_safe_int(stats.get("tokens_in")),
        stats_tokens_out=_safe_int(stats.get("tokens_out")),
        stats_cost_usd=_safe_float(stats.get("cost_usd")),
        stats_turns=_safe_int(stats.get("turns")),
        stats_tool_calls=_safe_int(stats.get("tool_calls")),
        stats_errors=_safe_int(stats.get("errors")),
        stale=stale,
    )


def _safe_int(v: Any) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


class LocalInstancesProvider:
    """Lê `<runtime_dir>/*.json` e devolve snapshots por PID.

    Cada arquivo é o state file de uma instância DEILE rodando no host
    (publicado pelo Agent A — issue #303). O provider parseia, valida o
    `schema_version`, e faz GC silencioso de arquivos órfãos (PID morto).

    Async-First (princípio §1): `_fetch` faz I/O síncrono (listdir +
    file reads pequenos, ~1KB cada). Aceitável porque o cache TTL é
    chamado em thread separada (`BackgroundRefresher`) — nunca bloqueia
    o render. Mesma convenção dos demais providers locais.

    Trade-off: leituras não-atômicas (Agent A escreve `os.replace` ATÔMICO,
    mas leitor pode pegar a versão antiga ou nova entre frames — never
    parcial). JSON malformado é skipped + logged WARN; não derruba o
    diretório inteiro.

    Cache TTL default 2s — state files mudam rápido (heartbeat ~2s + cada
    tool execution dispara update). Maior que isso e a UI fica visivelmente
    parada; menor e martelaria o filesystem.
    """

    def __init__(self, runtime_dir: Optional[Path] = None,
                 ttl_s: float = 2.0,
                 stale_after_s: float = _INSTANCE_STALE_AFTER_S):
        # Resolução lazy: env var > param > default global. Faz lookup no
        # construtor (não no import) para os testes poderem monkeypatchar
        # `DEILE_RUNTIME_DIR` antes de instanciar o provider.
        if runtime_dir is None:
            env = os.environ.get("DEILE_RUNTIME_DIR")
            runtime_dir = Path(env) if env else RUNTIME_DIR
        self._runtime_dir = runtime_dir
        self._stale_after_s = stale_after_s
        self._cache: Cache[Dict[int, InstanceSnapshot]] = Cache(
            ttl_s, self._fetch, fallback={},
        )

    @property
    def runtime_dir(self) -> Path:
        return self._runtime_dir

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> Dict[int, InstanceSnapshot]:
        """Mapa PID → snapshot. Vazio quando o diretório não existe."""
        return self._cache.get(force)

    def _fetch(self) -> Dict[int, InstanceSnapshot]:
        if not self._runtime_dir.is_dir():
            return {}
        out: Dict[int, InstanceSnapshot] = {}
        now = datetime.now(_UTC)
        # `glob('*.json')` evita carregar arquivos temporários que Agent A
        # use durante o `os.replace` atômico (tmp + rename, padrão POSIX).
        try:
            entries = list(self._runtime_dir.glob("*.json"))
        except OSError as exc:
            # Permissão / FS corrompido — propaga como last_error.
            raise RuntimeError(f"listdir {self._runtime_dir}: {exc}") from exc
        for path in entries:
            snap = self._load_one(path, now=now)
            if snap is None:
                continue
            # Conflito (dois state files apontando o mesmo PID — caso muito
            # raro de race entre crash + restart muito rápido) é resolvido
            # mantendo o de heartbeat mais recente.
            existing = out.get(snap.pid)
            if existing is None:
                out[snap.pid] = snap
                continue
            old_hb = existing.last_heartbeat_at
            new_hb = snap.last_heartbeat_at
            if new_hb is not None and (old_hb is None or new_hb > old_hb):
                out[snap.pid] = snap
        return out

    def _load_one(self, path: Path,
                  *, now: datetime) -> Optional[InstanceSnapshot]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            # Arquivo pode ter sumido entre `glob` e `read` — não loga
            # como warning, é caso normal de Agent A reescrevendo.
            logger.debug("instance file vanished: %s (%s)", path, exc)
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "instance file malformed, skipping: %s (%s)", path, exc,
            )
            return None
        if not isinstance(payload, dict):
            logger.warning(
                "instance file not a JSON object, skipping: %s", path,
            )
            return None
        # Schema-check ANTES de mexer em PID — versões futuras podem ter
        # renomeado `pid` e ainda assim ser "válidas" pra Agent A.
        try:
            schema_version = int(payload.get("schema_version", 0))
        except (TypeError, ValueError):
            schema_version = 0
        if schema_version != _INSTANCE_SCHEMA_VERSION:
            logger.warning(
                "instance file schema_version=%s unsupported (expected %s): %s",
                schema_version, _INSTANCE_SCHEMA_VERSION, path,
            )
            return None
        snap = _snapshot_from_payload(
            payload, now=now, stale_after_s=self._stale_after_s,
        )
        if snap is None:
            logger.warning(
                "instance file invalid payload (no pid?), skipping: %s", path,
            )
            return None
        # GC: PID morto → unlink silencioso e skip. Best-effort: se o
        # unlink falhar (permissão, FS readonly), apenas pulamos o
        # snapshot — o próximo tick tenta de novo.
        if not _pid_alive(snap.pid):
            try:
                path.unlink()
            except OSError as exc:
                logger.debug(
                    "GC unlink failed for %s: %s (will retry)", path, exc,
                )
            return None
        return snap


# ===== Aggregate hub ========================================================

@dataclass
class PanelData:
    """Conjunto de providers consumidos pela UI.

    `context` é a fonte única de verdade da configuração (namespace,
    paths, modo). Providers k8s sempre presentes (falham gracioso se
    cluster ausente); locais opcionais (None quando não detectados).
    """
    context: RuntimeContext
    pods: PodsProvider
    pipeline: PipelineProvider
    workers: WorkerProvider
    github: GitHubProvider
    costs: CostsProvider
    models: ModelsProvider
    current_model: CurrentModelProvider
    notifier: NotifierProvider
    local_processes: Optional[LocalProcessesProvider] = None
    local_logs: Optional[LocalLogsProvider] = None
    local_audit: Optional[LocalAuditProvider] = None
    # Per-PID `current_action` (issue #303). Quando presente, o adapter
    # `_local_process_rows` prefere este provider ao fallback do log
    # global — assim cada processo DEILE mostra seu próprio "doing now"
    # em vez do mesmo texto compartilhado.
    local_instances: Optional["LocalInstancesProvider"] = None

    @classmethod
    def from_context(cls, context: RuntimeContext) -> "PanelData":
        """Constrói o aggregator usando overrides do `RuntimeContext`.

        Locais são instanciados apenas se `context.local_available` —
        evita ler `ps` ou abrir arquivos quando o operador forçou
        `--k8s-only` (ou está em demo).
        """
        local_on = context.local_available
        local_procs = LocalProcessesProvider() if local_on else None
        local_logs = (LocalLogsProvider(context.logs_dir / "deile.log")
                      if local_on else None)
        local_audit = (LocalAuditProvider(context.logs_dir / "security_audit.log")
                       if local_on else None)
        # `LocalInstancesProvider` é independente da existência do dir
        # — se vazio, retorna {} (sem erro). Por isso pendurado no mesmo
        # `local_on` que os demais (operador em --k8s-only não quer ver
        # nada vindo do host).
        local_instances = LocalInstancesProvider() if local_on else None
        # `enabled=context.k8s_available` faz os providers k8s curto-circuitarem
        # via `_check_enabled`/`Cache.fallback` quando o modo é local-only
        # (sem custo de subprocess `kubectl`).
        k8s_on = context.k8s_available
        return cls(
            context=context,
            pods=PodsProvider(namespace=context.namespace, enabled=k8s_on),
            pipeline=PipelineProvider(namespace=context.namespace,
                                      deploy=context.pipeline_deploy,
                                      enabled=k8s_on),
            workers=WorkerProvider(namespace=context.namespace,
                                   worker_deploy=context.worker_deploy,
                                   enabled=k8s_on),
            # `context.repo` pode vir vazio se o operador construiu o
            # ctx direto (sem `.detect()`) — resolve no fallback global.
            github=GitHubProvider(repo=context.repo or REPO_DEFAULT),
            costs=CostsProvider(db_path=context.usage_db),
            models=ModelsProvider(),
            current_model=CurrentModelProvider(
                namespace=context.namespace,
                deployments=(context.worker_deploy, context.pipeline_deploy),
                enabled=k8s_on,
            ),
            notifier=NotifierProvider(namespace=context.namespace,
                                      deploy=context.bot_deploy,
                                      enabled=k8s_on),
            local_processes=local_procs,
            local_logs=local_logs,
            local_audit=local_audit,
            local_instances=local_instances,
        )

    @classmethod
    def default(cls, repo: str = REPO_DEFAULT) -> "PanelData":
        """Backwards-compat: contexto padrão (namespace `deile`)."""
        return cls.from_context(RuntimeContext.detect(repo=repo))

    def _all_providers(self) -> tuple:
        """Ordem usada por `force_refresh_all` e `errors`."""
        base = (self.pods, self.pipeline, self.workers, self.github,
                self.costs, self.models, self.current_model, self.notifier)
        locals_ = tuple(p for p in (self.local_processes, self.local_logs,
                                    self.local_audit, self.local_instances)
                        if p is not None)
        return base + locals_

    def force_refresh_all(self) -> None:
        """Hotkey [r]: marca todos os caches como vencidos sem bloquear.

        O ``BackgroundRefresher`` pega no próximo tick (≤0.5s) e refaz.
        Enquanto isso, ``get()`` continua devolvendo o valor velho — a UI
        nunca trava.
        """
        for p in self._all_providers():
            p._cache.invalidate()  # noqa: SLF001 (intentional internal)

    def errors(self) -> List[tuple]:
        """Lista provider name + último erro, pra UI mostrar discretamente.

        Filtra erros "esperados" (`k8s desabilitado` em modo local-only e
        `kubectl não encontrado` quando o operador não tem cluster) —
        eles seriam ruído visual no painel ALERTS sem trazer informação.
        """
        names = ["pods", "pipeline", "workers", "github", "costs",
                 "models", "current_model", "notifier"]
        if self.local_processes is not None:
            names.append("local_processes")
        if self.local_logs is not None:
            names.append("local_logs")
        if self.local_audit is not None:
            names.append("local_audit")
        if self.local_instances is not None:
            names.append("local_instances")
        out: List[tuple] = []
        for name, p in zip(names, self._all_providers()):
            err = p.last_error
            if not err:
                continue
            # "k8s desabilitado" e "kubectl não encontrado" são esperados —
            # não viram alerta.
            if "k8s desabilitado" in err or "kubectl não encontrado" in err:
                continue
            out.append((name, err))
        return out


# ===== BackgroundRefresher ==================================================

class BackgroundRefresher:
    """Thread daemon que mantém os caches de ``PanelData`` frescos sem
    bloquear o thread principal.

    Roda ``maybe_refresh`` de cada provider em paralelo via thread pool
    (até 8 ao mesmo tempo, pra um refresh único do gh API não atrasar o
    do kubectl que é mais rápido). Cada provider tem seu próprio TTL —
    o refresher não dispara fetch antes da hora.

    Pensado para iniciar via ``with``/``start()`` no ``PanelApp.run`` e
    parar no ``__exit__``/``stop()``. Daemon=True: morre junto com o
    processo se algo der errado.
    """

    DEFAULT_TICK_S = 0.5  # frequência de checagem; fetch real respeita TTL

    def __init__(self, data: PanelData, tick_s: float = DEFAULT_TICK_S):
        self._data = data
        self._tick_s = tick_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Pool dimensionado para acomodar todos os providers em paralelo
        # (8 hoje), assim um fetch lento não bloqueia os rápidos.
        self._pool = futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="panel-bg",
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="panel-refresher",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        # `cancel_futures=True` evita esperar fetches já enfileirados.
        self._pool.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "BackgroundRefresher":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _loop(self) -> None:
        # `maybe_refresh` vive no Cache, não no Provider; cada provider
        # expõe o cache em `._cache` por convenção.
        providers = self._data._all_providers()  # noqa: SLF001
        while not self._stop.is_set():
            futs = [self._pool.submit(p._cache.maybe_refresh)  # noqa: SLF001
                    for p in providers]
            # Aguarda concluir (ou stop ser sinalizado) — sem bloquear
            # eternamente caso um fetcher pendure.
            for f in futs:
                if self._stop.is_set():
                    break
                try:
                    f.result(timeout=20.0)
                except Exception:  # noqa: BLE001 (defensive — never crash)
                    pass
            self._stop.wait(timeout=self._tick_s)
