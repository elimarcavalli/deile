"""Data providers do painel TUI — kubectl, gh, SQLite — com cache TTL.

Cada provider expõe um `get()` que devolve um dataclass tipado e fica
silenciosamente vazio quando a fonte está indisponível (cluster down, gh
sem auth, DB ausente). A camada de view (`_panel.py`) lê dos `get()` sem
saber se veio do cluster ou do fallback.

Cache: cada provider tem `Cache[T]` com TTL próprio (3-60s). `get(force=True)`
re-busca; chamadas posteriores dentro do TTL retornam o valor cacheado.
Não usa threads — a leitura é lazy, o custo de cada `kubectl/gh/sqlite` é
absorvido pelo loop principal do `PanelApp` que renderiza em cadência
calma (3s default).

Fontes:
- pods + recursos:  `kubectl -n deile get pods -o json`
- pipeline:         `kubectl logs deploy/deile-pipeline --tail=200 --timestamps`
- worker (por pod): `kubectl logs <pod> --tail=200 --timestamps`
- issues + PRs:     `gh api /repos/<repo>/issues?state=open`
- custos:           `~/.deile/db/usage.db` (UsageRepository)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import threading
import time
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


# ===== cache ================================================================

@dataclass
class Cache(Generic[T]):
    """Cache TTL ao redor de um fetcher.

    Em caso de erro, mantém o último valor bom; se nunca houve valor bom,
    devolve o `fallback`. Guarda a última exceção em `last_error` para a
    view exibir um indicador discreto.
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
        now = time.monotonic()
        fresh_enough = (
            self._value is not None
            and (now - self._fetched_at) < self.ttl_s
        )
        if fresh_enough and not force:
            return self._value  # type: ignore[return-value]
        try:
            new = self.fetcher()
        except Exception as exc:  # noqa: BLE001 (defensive — render must never crash)
            # Falhas crônicas devem ficar visíveis nos logs do operador (debug
            # apenas — alarmar a cada tick poluiria; last_error já é exibido
            # discretamente na UI).
            logger.debug(
                "Cache.get fetcher failed: %s", exc, exc_info=True,
            )
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            if self._value is None:
                return self.fallback
            return self._value
        with self._lock:
            self._value = new
            self._fetched_at = now
            self._last_error = None
        return new


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


class PodsProvider:
    """Lista os pods do namespace `deile` em forma tipada."""

    def __init__(self, ttl_s: float = 3.0):
        self._kubectl = kubectl_bin()
        self._cache: Cache[List[PodInfo]] = Cache(ttl_s, self._fetch, fallback=[])

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> List[PodInfo]:
        return self._cache.get(force)

    def _resolve_kubectl(self) -> Optional[str]:
        """Re-resolve kubectl lazy — operador pode tê-lo instalado depois
        do painel ter sido aberto."""
        if self._kubectl is None:
            self._kubectl = kubectl_bin()
        return self._kubectl

    def _fetch(self) -> List[PodInfo]:
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        data = _capture_json(
            [self._kubectl, "-n", NS, "get", "pods", "-o", "json"],
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


class PipelineProvider:
    """Lê os últimos N segundos de log do deile-pipeline e classifica."""

    DEPLOY = "deile-pipeline"
    TAIL_LINES = 200

    def __init__(self, ttl_s: float = 5.0):
        self._kubectl = kubectl_bin()
        self._cache: Cache[PipelineState] = Cache(
            ttl_s, self._fetch, fallback=PipelineState(),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> PipelineState:
        return self._cache.get(force)

    def _resolve_kubectl(self) -> Optional[str]:
        if self._kubectl is None:
            self._kubectl = kubectl_bin()
        return self._kubectl

    def _fetch(self) -> PipelineState:
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        text = _capture_text(
            [self._kubectl, "-n", NS, "logs",
             f"deploy/{self.DEPLOY}",
             f"--tail={self.TAIL_LINES}", "--timestamps"],
            timeout=5.0,
        )
        if text is None:
            raise RuntimeError(f"kubectl logs {self.DEPLOY} falhou")
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


class WorkerProvider:
    """Por pod worker, deduz busy/idle do log."""

    LABEL_SELECTOR = "app=deile-worker"
    TAIL_LINES = 200

    def __init__(self, ttl_s: float = 5.0):
        self._kubectl = kubectl_bin()
        self._cache: Cache[Dict[str, WorkerState]] = Cache(
            ttl_s, self._fetch, fallback={},
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> Dict[str, WorkerState]:
        return self._cache.get(force)

    def _resolve_kubectl(self) -> Optional[str]:
        if self._kubectl is None:
            self._kubectl = kubectl_bin()
        return self._kubectl

    def _fetch(self) -> Dict[str, WorkerState]:
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        names_raw = _capture_text(
            [self._kubectl, "-n", NS, "get", "pods",
             "-l", self.LABEL_SELECTOR,
             "-o", "jsonpath={.items[*].metadata.name}"],
            timeout=4.0,
        )
        if names_raw is None:
            raise RuntimeError("kubectl get worker pods falhou")
        pod_names = [n for n in names_raw.split() if n]
        states: Dict[str, WorkerState] = {}
        for name in pod_names:
            text = _capture_text(
                [self._kubectl, "-n", NS, "logs", name,
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

    def __init__(self, db_path: Path = USAGE_DB, ttl_s: float = 60.0):
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


class CurrentModelProvider:
    """Lê o `DEILE_PREFERRED_MODEL` setado no Deployment do worker (e/ou
    pipeline) — o valor que efetivamente roda agora nos pods."""

    DEFAULT_DEPLOYMENTS = ("deile-worker", "deile-pipeline")

    def __init__(self, deployments=DEFAULT_DEPLOYMENTS, ttl_s: float = 5.0):
        self._kubectl = kubectl_bin()
        self._deployments = tuple(deployments)
        self._cache: Cache[Dict[str, Optional[str]]] = Cache(
            ttl_s, self._fetch, fallback={d: None for d in deployments},
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> Dict[str, Optional[str]]:
        return self._cache.get(force)

    def _resolve_kubectl(self) -> Optional[str]:
        if self._kubectl is None:
            self._kubectl = kubectl_bin()
        return self._kubectl

    def _fetch(self) -> Dict[str, Optional[str]]:
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        out: Dict[str, Optional[str]] = {}
        for dep in self._deployments:
            data = _capture_json(
                [self._kubectl, "-n", NS, "get",
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
) -> None:
    """Emite AuditEvent(SECURITY_POLICY_CHANGED) para a troca de modelo.

    Silencioso se o pacote deile não estiver importável (e.g., rodando
    o painel em isolamento sem o módulo principal no sys.path). A falha
    de log NUNCA bloqueia a ação — mas a falha de validação sim.
    """
    try:
        from deile.security.audit_logger import (  # noqa: PLC0415
            AuditEvent, AuditEventType, SeverityLevel, get_audit_logger,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit logger indisponível para set_preferred_model: %s", exc,
        )
        return
    try:
        get_audit_logger().log_event(
            event_type=AuditEventType.SECURITY_POLICY_CHANGED,
            severity=SeverityLevel.WARNING,
            actor="panel:set_preferred_model",
            resource=f"deployment:{NS}/{deployment}:DEILE_PREFERRED_MODEL",
            action="kubectl_set_env",
            result=result,
            details={"slug": slug, "detail": detail[:200]},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("falha emitindo AuditEvent: %s", exc)


def set_preferred_model(deployment: str, slug: str,
                        timeout: float = 15.0) -> tuple:
    """Aplica `DEILE_PREFERRED_MODEL=<slug>` no Deployment.

    `kubectl set env` modifica a spec do Deployment, o que dispara
    rollout automático (com a strategy do manifest — `RollingUpdate`
    para o worker e `Recreate` para o pipeline). Retorna `(ok, msg)`.

    Slug é validado contra `_MODEL_SLUG_RE` antes de virar argv — rejeita
    quebras de linha, `=`, NUL, controle e espaços que permitiriam
    injeção em argumentos do kubectl. Toda chamada (allowed/denied/falha)
    emite `AuditEvent(SECURITY_POLICY_CHANGED)`.
    """
    if not isinstance(slug, str) or not _MODEL_SLUG_RE.match(slug):
        _audit_security_policy_change(
            deployment, slug if isinstance(slug, str) else repr(slug),
            result="denied", detail="slug inválido",
        )
        return False, "slug inválido — recusado por validação"
    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_security_policy_change(
            deployment, slug, result="failed", detail="kubectl não encontrado",
        )
        return False, "kubectl não encontrado"
    # Audit ANTES de executar — registra a intenção, ainda que o
    # subprocess depois falhe ou trave.
    _audit_security_policy_change(
        deployment, slug, result="allowed", detail="executando kubectl set env",
    )
    try:
        proc = subprocess.run(
            [kubectl, "-n", NS, "set", "env", f"deploy/{deployment}",
             f"DEILE_PREFERRED_MODEL={slug}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_security_policy_change(
            deployment, slug, result="failed", detail=f"subprocess: {exc}",
        )
        return False, f"falha ao executar kubectl: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        _audit_security_policy_change(
            deployment, slug, result="failed",
            detail=f"rc={proc.returncode} {err}",
        )
        return False, err
    msg = (proc.stdout or "rollout disparado").strip()
    _audit_security_policy_change(
        deployment, slug, result="completed", detail=msg,
    )
    return True, msg


# ===== Notifier (bot audit log) =============================================

class NotifierProvider:
    """Tail dos audit events do `deilebot` via kubectl logs.

    Sai como `List[str]` (linhas cruas) — a view filtra/parsea. Cache TTL
    5s evita o `kubectl logs deploy/deilebot --tail=500` por render (a
    NotifierEchoView refresha em 5s; era 12 chamadas/min sem cache).
    """

    BOT_DEPLOY = "deilebot"
    TAIL = 500

    def __init__(self, ttl_s: float = 5.0):
        self._kubectl = kubectl_bin()
        self._cache: Cache[List[str]] = Cache(ttl_s, self._fetch, fallback=[])

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> List[str]:
        return self._cache.get(force)

    def _resolve_kubectl(self) -> Optional[str]:
        if self._kubectl is None:
            self._kubectl = kubectl_bin()
        return self._kubectl

    def _fetch(self) -> List[str]:
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        text = _capture_text(
            [self._kubectl, "-n", NS, "logs",
             f"deploy/{self.BOT_DEPLOY}", f"--tail={self.TAIL}"],
            timeout=5.0,
        )
        if text is None:
            raise RuntimeError("kubectl logs deilebot falhou")
        if len(text) > MAX_LOG_BYTES:
            text = text[-MAX_LOG_BYTES:]
        return text.splitlines()


# ===== Aggregate hub ========================================================

@dataclass
class PanelData:
    """Conjunto de providers consumidos pela UI."""
    pods: PodsProvider
    pipeline: PipelineProvider
    workers: WorkerProvider
    github: GitHubProvider
    costs: CostsProvider
    models: ModelsProvider
    current_model: CurrentModelProvider
    notifier: NotifierProvider

    @classmethod
    def default(cls, repo: str = REPO_DEFAULT) -> "PanelData":
        return cls(
            pods=PodsProvider(),
            pipeline=PipelineProvider(),
            workers=WorkerProvider(),
            github=GitHubProvider(repo=repo),
            costs=CostsProvider(),
            models=ModelsProvider(),
            current_model=CurrentModelProvider(),
            notifier=NotifierProvider(),
        )

    def force_refresh_all(self) -> None:
        """Usado pelo hotkey [r]: invalida todos os caches no próximo `get`."""
        for p in (self.pods, self.pipeline, self.workers,
                  self.github, self.costs, self.models, self.current_model,
                  self.notifier):
            p._cache.invalidate()  # noqa: SLF001 (público no Cache; provider ainda é pacote)

    def errors(self) -> List[tuple]:
        """Lista provider name + último erro, pra UI mostrar discretamente."""
        out: List[tuple] = []
        for name, p in (
            ("pods", self.pods),
            ("pipeline", self.pipeline),
            ("workers", self.workers),
            ("github", self.github),
            ("costs", self.costs),
            ("models", self.models),
            ("current_model", self.current_model),
            ("notifier", self.notifier),
        ):
            if p.last_error:
                out.append((name, p.last_error))
        return out
