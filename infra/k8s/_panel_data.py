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
from typing import Any, Callable, Dict, Generic, List, Optional, Set, Tuple, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

# Namespace padrão: lido do env DEILE_K8S_NAMESPACE; fallback "deile".
# Providers usam `RuntimeContext.namespace`, nunca esta constante diretamente.
NS = os.environ.get("DEILE_K8S_NAMESPACE", "deile")
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
# Deployments do stack inteiro (alvos das ações de ciclo-de-vida do painel:
# delete pod, rollout restart). Distinto de _ALLOWED_DEPLOYMENTS, que cobre
# só a troca de modelo (worker + pipeline). Aqui incluímos também bot/shell.
_ALLOWED_DEPLOYMENTS_FULL = frozenset({
    "deile-pipeline", "deile-worker", "deilebot", "deile-shell",
    # issue #309 fase 2 — claude-worker entrou no stack como pod paralelo
    # (Service :8767, executa ``claude -p``). Faltava no whitelist e
    # qualquer ação de painel contra ele era rejeitada com "fora da
    # whitelist". Cobre delete pod, rollout restart, tmp resize.
    "claude-worker",
})
# Pod name: snowflake-ish letras-minúsculas + dígitos + hífen (validação
# leve para argv); até 253 chars (limite DNS-1123). Validado antes de
# chegar em qualquer ``kubectl delete pod`` — sem espaços, sem barra,
# sem flags.
_POD_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,251}[a-z0-9])?$")


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

# Label aplicada a namespaces gerenciados pelo DEILE (mesma do deploy.py).
_DEILE_NS_LABEL = "app.kubernetes.io/managed-by=deile"


def discover_deile_namespaces() -> List[str]:
    """Enumera namespaces DEILE acessíveis no cluster atual.

    Estratégia combinada (mesmo critério de `k8s list` no deploy.py):
    1. Namespaces com label `app.kubernetes.io/managed-by=deile`.
    2. Fallback: namespaces que têm pods com `app=deile-pipeline`.

    Devolve lista ordenada. Retorna lista vazia se kubectl ausente,
    cluster inacessível ou nenhum namespace encontrado — o chamador
    decide o que exibir.
    """
    kubectl = kubectl_bin()
    if kubectl is None:
        return []
    try:
        by_label = subprocess.run(
            [kubectl, "get", "ns", "-l", _DEILE_NS_LABEL,
             "-o", "jsonpath={.items[*].metadata.name}"],
            capture_output=True, text=True, timeout=5.0,
        )
        labeled: set = set()
        if by_label.returncode == 0 and by_label.stdout.strip():
            labeled = set(by_label.stdout.strip().split())
    except (OSError, subprocess.TimeoutExpired):
        labeled = set()

    try:
        by_pod = subprocess.run(
            [kubectl, "get", "pods", "--all-namespaces",
             "-l", "app=deile-pipeline",
             "-o", "jsonpath={.items[*].metadata.namespace}"],
            capture_output=True, text=True, timeout=5.0,
        )
        from_pods: set = set()
        if by_pod.returncode == 0 and by_pod.stdout.strip():
            from_pods = set(by_pod.stdout.strip().split())
    except (OSError, subprocess.TimeoutExpired):
        from_pods = set()

    return sorted(labeled | from_pods)
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
    layout `infra/k8s/manifests/` (namespace padrão = ``NS``, deployments
    `deile-pipeline`/`deile-worker`/`deilebot`/`deile-shell`).

    Use `RuntimeContext.detect(**overrides)` para construir aplicando
    overrides do CLI (`--namespace`, `--pipeline-deploy`, etc).
    """

    # NS é o namespace default do painel: env DEILE_K8S_NAMESPACE ou "deile".
    namespace: str = field(default_factory=lambda: NS)
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
    # forge_kind: "github" | "gitlab" | "" (auto-detect ou indisponível).
    # Lido do env DEILE_FORGE_KIND no deployment deile-pipeline, se possível.
    forge_kind: str = ""
    k8s_force: bool = False
    local_force: bool = False
    demo: bool = False

    @classmethod
    def detect(cls, **overrides: Any) -> "RuntimeContext":
        """Constrói o contexto, resolvendo defaults quando overrides faltam.

        Ordem de resolução do ``repo``:

        1. ``overrides["repo"]`` — operador declarou no CLI.
        2. ``_read_forge_repo(ns)`` — ConfigMap ``deile-runtime-config`` do
           NS escolhido. **Sempre** consultado quando há kubectl e o NS
           difere do default; permite o painel apontar ao repo correto
           num NS GitLab (``_detect_default_repo`` só conhece github.com).
        3. ``_detect_default_repo()`` — ``git remote get-url origin``
           local; fallback histórico.
        """
        ns_choice = overrides.get("namespace") or NS
        repo = overrides.pop("repo", None) or ""
        if not repo:
            repo = _read_forge_repo(ns_choice) or _detect_default_repo()
        # Tenta ler o forge_kind do cluster se não foi passado como override.
        if "forge_kind" not in overrides:
            overrides["forge_kind"] = _read_forge_kind(ns_choice)
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


def _read_forge_repo(namespace: str) -> str:
    """Lê ``pipeline.repo`` do ConfigMap ``deile-runtime-config`` do NS.

    Quando o painel aponta para um namespace que carrega forge GitLab, o
    repo certo a listar **não** é o ``owner/repo`` do clone local (que
    ``_detect_default_repo()`` retornaria via ``gh repo view``) — é o repo
    que o pipeline está orquestrando, declarado no ConfigMap layered.
    Falha graciosamente para ``""`` (callsite cai no default).
    """
    kubectl = kubectl_bin()
    if kubectl is None:
        return ""
    try:
        out = subprocess.run(
            [kubectl, "-n", namespace, "get", "configmap",
             "deile-runtime-config",
             "-o", "jsonpath={.data.pipeline-settings\\.json}"],
            capture_output=True, text=True, timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0 or not out.stdout.strip():
        return ""
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return ""
    pipeline = payload.get("pipeline") if isinstance(payload, dict) else None
    if isinstance(pipeline, dict):
        repo = pipeline.get("repo")
        if isinstance(repo, str) and repo.strip():
            return repo.strip()
    return ""


def _read_forge_kind(namespace: str) -> str:
    """Lê o env DEILE_FORGE_KIND do deploy deile-pipeline no namespace dado.

    Retorna "github" | "gitlab" | "" (auto/erro). Faz uma única chamada
    kubectl silenciosa; nunca lança exceção — fallback é "".
    """
    kubectl = kubectl_bin()
    if kubectl is None:
        return ""
    try:
        out = subprocess.run(
            [kubectl, "-n", namespace, "get", "deploy", "deile-pipeline",
             "-o", "jsonpath={.spec.template.spec.containers[0].env}"],
            capture_output=True, text=True, timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0 or not out.stdout.strip():
        return ""
    # O jsonpath devolve o array de env vars como JSON-like; procura DEILE_FORGE_KIND.
    try:
        envs = json.loads(out.stdout)
        for entry in envs:
            if isinstance(entry, dict) and entry.get("name") == "DEILE_FORGE_KIND":
                return (entry.get("value") or "").strip().lower()
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


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


# ===== Resource / metrics helpers ==========================================

_MEM_SUFFIXES: Dict[str, int] = {
    "Ki": 1024,
    "Mi": 1024 ** 2,
    "Gi": 1024 ** 3,
    "Ti": 1024 ** 4,
    "K":  1000,
    "M":  1000 ** 2,
    "G":  1000 ** 3,
}


def _parse_cpu(s: str) -> Optional[int]:
    """'230m' → 230 (millicores); '2' → 2000."""
    if not s:
        return None
    if s.endswith("m"):
        try:
            return int(s[:-1])
        except ValueError:
            return None
    try:
        return int(float(s) * 1000)
    except ValueError:
        return None


def _parse_mem(s: str) -> Optional[int]:
    """'412Mi' → bytes.  Supports Ki/Mi/Gi/Ti and K/M/G."""
    if not s:
        return None
    for suffix, mult in _MEM_SUFFIXES.items():
        if s.endswith(suffix):
            try:
                return int(s[: -len(suffix)]) * mult
            except ValueError:
                return None
    try:
        return int(s)
    except ValueError:
        return None


def _fmt_mem_display(b: Optional[int]) -> str:
    if not isinstance(b, int):
        return "?"
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f}Gi"
    if b >= 1024 ** 2:
        return f"{b // 1024 ** 2}Mi"
    if b >= 1024:
        return f"{b // 1024}Ki"
    return f"{b}B"


def _fmt_cpu_display(mc: Optional[int]) -> str:
    return "?" if not isinstance(mc, int) else f"{mc}m"


def _pct(used: Optional[int], limit: Optional[int]) -> Optional[float]:
    if not isinstance(used, int) or not isinstance(limit, int) or limit == 0:
        return None
    return used / limit * 100


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
    # OOM history (populated from containerStatuses[].lastState.terminated)
    oom_killed_count: int = 0
    last_oom_at: Optional[datetime] = None
    # Resource limits from spec.containers[].resources.limits (aggregated)
    cpu_limit_millicores: Optional[int] = None
    mem_limit_bytes: Optional[int] = None
    # Live usage from kubectl top pod (injected by PodMetricsProvider)
    cpu_millicores: Optional[int] = None
    mem_bytes: Optional[int] = None


_ROLE_BY_APP = {
    "deile-pipeline": "pipeline",
    "deile-worker":   "worker",
    "deilebot":       "bot",
    "deile-shell":    "shell",
    # issue #396: claude-worker pods are now observable in PodWatchView
    "claude-worker":  "claude-worker",
}


class PodsProvider(_KubectlProviderMixin):
    """Lista os pods do namespace em forma tipada (default = NS)."""

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

            # OOM history: aggregate across all containers.
            oom_count = 0
            last_oom: Optional[datetime] = None
            for cs in container_statuses:
                for state_block in (
                    cs.get("lastState", {}).get("terminated", {}),
                    cs.get("state", {}).get("terminated", {}),
                ):
                    if state_block.get("reason") == "OOMKilled":
                        oom_count += 1
                        fin = _parse_k8s_ts(state_block.get("finishedAt"))
                        if fin is not None and (
                            last_oom is None or fin > last_oom
                        ):
                            last_oom = fin

            # Resource limits: aggregate across all containers.
            containers = item.get("spec", {}).get("containers", []) or []
            cpu_limit_total = 0
            mem_limit_total = 0
            for c in containers:
                lims = c.get("resources", {}).get("limits", {})
                cpu_val = _parse_cpu(lims.get("cpu", ""))
                mem_val = _parse_mem(lims.get("memory", ""))
                if cpu_val is not None:
                    cpu_limit_total += cpu_val
                if mem_val is not None:
                    mem_limit_total += mem_val

            rows.append(PodInfo(
                name=meta.get("name", "?"),
                role=role,
                status=phase,
                ready=ready,
                restarts=restarts,
                age_s=age_s,
                started_at=started_at,
                node=item.get("spec", {}).get("nodeName", ""),
                oom_killed_count=oom_count,
                last_oom_at=last_oom,
                cpu_limit_millicores=cpu_limit_total if cpu_limit_total else None,
                mem_limit_bytes=mem_limit_total if mem_limit_total else None,
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
        """Timestamp em UTC explícito com sufixo Z (ex: '16:33:45Z')."""
        return self.ts.strftime("%H:%M:%SZ")

    @property
    def hhmmss_local(self) -> str:
        """Conversão para hora local como linha auxiliar (ex: '13:33 -03')."""
        local = self.ts.astimezone()
        return local.strftime("%H:%M %z")


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
        # Pre-check: ``kubectl logs deploy/<name>`` espera até 20s por um pod
        # quando o deployment está com 0 replicas (scale stop). No painel,
        # ``_capture_text`` corta em 5s e devolve ``None`` — cada refresh
        # tick virava 5s travados, o que o operador percebe como painel
        # "lento". Consulta barata (~50ms) ao spec.replicas evita o timeout
        # e devolve um ``PipelineState`` vazio rapidamente.
        replicas_text = _capture_text(
            [self._kubectl, "-n", self._namespace, "get",
             f"deploy/{self._deploy}",
             "-o", "jsonpath={.spec.replicas}"],
            timeout=2.0,
        )
        if replicas_text is not None and replicas_text.strip() in ("0", ""):
            # Deploy parado (scale=0) ou ausente — devolve estado vazio.
            return PipelineState()
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
class CurrentTask:
    """Snapshot da task em execução em um pod worker.

    Populada pelo :class:`WorkerProvider` quando ele encontra uma linha
    ``dispatch_started`` no log do worker sem um ``dispatch_completed``
    correspondente — i.e., a última dispatch ainda está rodando. Surface
    primária: cabeçalho do :class:`PodWatchView` no painel TUI (issue
    #309 fase 2 follow-up), que mostra ao operador "o que esse worker
    está fazendo agora" sem precisar interpretar o log bruto.

    Forge-agnóstica: ``issue_number`` é apenas um inteiro (sem ``#``/
    ``!``); o renderer da UI escolhe o prefixo apropriado. ``channel_id``
    é mantido como fallback de display — quando o pipeline não envia
    ``issue_number`` explícito, o parser extrai o número do padrão
    ``pipeline-(issue|pr|mention-issue|mention-pr)-<N>`` do channel.

    Segurança (pilar 08): nenhum desses campos carrega ``brief``,
    histórico, credentials ou conteúdo do Discord — todos são metadata
    de roteamento já validada na fronteira do worker.
    """
    task_id: str
    channel_id: str
    started_ts: datetime
    stage: Optional[str] = None
    action_kind: Optional[str] = None
    issue_number: Optional[int] = None
    branch: Optional[str] = None
    model: Optional[str] = None

    @property
    def target_label(self) -> str:
        """Rótulo curto pro header (ex.: ``#309``, ``PR#291``, ``mention #257``).

        Convenção alinhada com :func:`_classify_pipeline_line` (mesmo
        vocabulário usado pelo :class:`PipelineTimelineView`). Quando
        ``issue_number`` foi enviado explicitamente, usa ele; senão tenta
        extrair do ``channel_id``. ``channel_id`` é a fonte canônica do
        pipeline (``pipeline-issue-N`` / ``pipeline-pr-N`` /
        ``pipeline-mention-{issue|pr}-N``); para dispatches não-pipeline
        (snowflake do Discord), devolve uma rotulagem genérica curta.
        """
        n = self.issue_number
        kind_hint = ""
        if self.channel_id:
            m = _PIPELINE_CHANNEL_RE.match(self.channel_id)
            if m:
                kind_hint = m.group(1)  # "issue" | "pr" | "mention-issue" | "mention-pr"
                if n is None:
                    try:
                        n = int(m.group(2))
                    except (ValueError, TypeError):
                        n = None
        if n is not None:
            # Ordem importa: ``mention-pr`` casa com ``endswith("pr")`` E
            # com ``startswith("mention-")``. Mention-routing precisa
            # ganhar para o operador ver explicitamente que veio de uma
            # mention (não de um dispatch direto da pipeline).
            if kind_hint.startswith("mention-"):
                kind_target = "PR" if kind_hint.endswith("pr") else "#"
                return f"mention {kind_target}{n}"
            if kind_hint.endswith("pr"):
                return f"PR#{n}"
            return f"#{n}"
        # Fallback (CLI/bot passthrough): mostra o channel_id truncado.
        return f"channel:{self.channel_id[:16]}"

    @property
    def elapsed_s(self) -> float:
        return (datetime.now(_UTC) - self.started_ts).total_seconds()


@dataclass
class LastCompletedTask:
    """Snapshot da última task finalizada num pod worker (issue #396).

    Populada pelo :class:`WorkerProvider` quando ele vê um
    ``dispatch_completed`` para o qual existe um ``dispatch_started``
    pareado em ``live_tasks``. Contém duração (calculada de
    ``started_ts → finished_ts``), outcome normalizado e custo USD
    (resolvido contra :class:`CostsProvider` por ``session_id``, ou
    ``None`` quando o ledger não tem entrada para esta task).

    Forge-agnóstica: reutiliza ``issue_number`` / ``stage`` /
    ``action_kind`` do :class:`CurrentTask` correspondente.
    """
    task_id: str
    channel_id: str
    finished_ts: datetime
    outcome: str            # "DONE" | "FAIL" | "APPROVE" | "REJECT" | etc.
    duration_s: float
    cost_usd: Optional[float]   # None = ledger unavailable
    stage: Optional[str] = None
    action_kind: Optional[str] = None
    issue_number: Optional[int] = None


@dataclass
class WorkerState:
    """Estado deduzido do log de um pod worker."""
    pod_name: str
    busy: bool = False
    last_dispatch_ts: Optional[datetime] = None
    last_substantive_ts: Optional[datetime] = None
    last_health_ts: Optional[datetime] = None
    last_substantive_body: str = ""
    # Task em execução agora (issue #309 fase 2 follow-up). ``None`` quando
    # o pod está idle ou quando o log não tem nenhum ``dispatch_started``
    # ativo (ainda não cobre todos os workers em deploy, então ``None`` é
    # o caminho silencioso de compatibilidade).
    current_task: Optional[CurrentTask] = None
    # Última task finalizada (issue #396). ``None`` quando o log de até
    # TAIL_LINES não contém nenhum ``dispatch_completed`` pareado.
    last_completed: Optional[LastCompletedTask] = None

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

# Structured dispatch markers emitted by ``infra.k8s.worker_server``
# (``dispatch_handler``) — single source of truth for the "what is this
# worker doing right now" header in :class:`PodWatchView`. Format must
# stay in sync with the ``logger.info`` calls there; key order is
# flexible (we extract by regex), but key NAMES are the wire contract.
_DISPATCH_STARTED_RE = re.compile(
    r"dispatch_started\s+(?P<kv>.+)$", re.IGNORECASE,
)
_DISPATCH_COMPLETED_RE = re.compile(
    r"dispatch_completed\s+task=(?P<task_id>[a-f0-9]+)(?P<rest>[^\n]*)$",
    re.IGNORECASE,
)
_KV_RE = re.compile(r"(\w+)=(\S+)")
# Pipeline channel naming convention (see implementer.py:_dispatch /
# stages.py mention routing): ``pipeline-(issue|pr|mention-issue|mention-pr)-<N>``.
# Used by :class:`CurrentTask` to extract the target number when the
# pipeline didn't pass ``issue_number`` explicitly (backward compat with
# older pipeline versions or non-pipeline callers).
_PIPELINE_CHANNEL_RE = re.compile(
    r"^pipeline-(mention-issue|mention-pr|issue|pr)-(\d+)$"
)


class WorkerProvider(_KubectlProviderMixin):
    """Por pod worker, deduz busy/idle do log."""

    LABEL_SELECTOR_FMT = "app={deploy}"
    TAIL_LINES = 200

    def __init__(self, ttl_s: float = 2.0,
                 namespace: str = NS, worker_deploy: str = "deile-worker",
                 enabled: bool = True,
                 costs: Optional["CostsProvider"] = None):
        # 2s: N `kubectl logs` (1 por worker) — só roda em background.
        self._kubectl = kubectl_bin()
        self._namespace = namespace
        self._worker_deploy = worker_deploy
        self._enabled = enabled
        # Optional: resolve cost_usd for LastCompletedTask (issue #396).
        self._costs = costs
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
        # Tracking de "task em execução agora" via pareamento de
        # ``dispatch_started`` ↔ ``dispatch_completed`` (issue #309 fase 2
        # follow-up). Mantemos um dict ``task_id -> CurrentTask`` enquanto
        # parseia o log; o ``current_task`` final é o último started ainda
        # vivo (mais recente). Logs antigos rotacionam o suficiente pra
        # que o dict não cresça indefinidamente — TAIL_LINES=200 limita.
        # Defensive: se um ``dispatch_started`` aparece sem o ``completed``
        # correspondente (worker reiniciado mid-task, log truncado, etc.),
        # o pareamento naturalmente reflete a realidade: a task "ficou
        # presa" como current_task até que o log rotacione, o que casa
        # com o comportamento observável do pod (busy/last_dispatch_ts
        # também ficariam congelados num dispatch antigo).
        live_tasks: Dict[str, CurrentTask] = {}
        for raw in text.splitlines():
            ll = _parse_log_line(raw)
            if ll is None:
                continue
            if _WORKER_HEALTH_RE.search(ll.body):
                state.last_health_ts = ll.ts
                continue
            # Pareamento started/completed — feito ANTES do dispatch-RE
            # genérico porque ambos casam com "dispatch" no body.
            m_done = _DISPATCH_COMPLETED_RE.search(ll.body)
            if m_done:
                tid = m_done.group("task_id")
                started_task = live_tasks.pop(tid, None)
                if started_task is not None:
                    # Build LastCompletedTask from the paired started entry.
                    kv_done = dict(_KV_RE.findall(m_done.group("rest")))
                    ok_raw = kv_done.get("ok", "")
                    outcome_raw = (kv_done.get("outcome")
                                   or kv_done.get("status")
                                   or ("DONE"
                                       if ok_raw.lower() in {"true", "1"}
                                       else "FAIL"))
                    duration_s = max(
                        0.0,
                        (ll.ts - started_task.started_ts).total_seconds(),
                    )
                    cost_usd: Optional[float] = None
                    if self._costs is not None:
                        try:
                            cost_usd = self._costs.get_task_cost(tid)
                        except Exception:
                            pass
                    state.last_completed = LastCompletedTask(
                        task_id=tid,
                        channel_id=started_task.channel_id,
                        finished_ts=ll.ts,
                        outcome=outcome_raw,
                        duration_s=duration_s,
                        cost_usd=cost_usd,
                        stage=started_task.stage,
                        action_kind=started_task.action_kind,
                        issue_number=started_task.issue_number,
                    )
                # Não é uma "atividade nova" — apenas o término de uma
                # task já contabilizada via ``dispatch_started``; pular o
                # restante do bookkeeping evita inflar last_substantive_ts
                # com o ack final.
                continue
            m_start = _DISPATCH_STARTED_RE.search(ll.body)
            if m_start:
                kv = dict(_KV_RE.findall(m_start.group("kv")))
                tid = kv.get("task", "")
                ch = kv.get("channel", "")
                if tid:
                    issue_num: Optional[int]
                    try:
                        issue_num = int(kv["issue"]) if "issue" in kv else None
                    except (ValueError, KeyError):
                        issue_num = None
                    live_tasks[tid] = CurrentTask(
                        task_id=tid, channel_id=ch, started_ts=ll.ts,
                        stage=kv.get("stage"),
                        action_kind=kv.get("kind"),
                        issue_number=issue_num,
                        branch=kv.get("branch"),
                        model=kv.get("model"),
                    )
                # ``dispatch_started`` é atividade substantiva — atualiza
                # last_substantive_ts/body também, para o cálculo de
                # "última atividade" não regredir.
                state.last_substantive_ts = ll.ts
                state.last_substantive_body = ll.body[:100]
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
        # ``current_task`` = task started mais recente ainda viva (sem
        # completed). Quando o pod está idle, ``live_tasks`` é {} e
        # current_task fica None — o renderer mostra "—".
        if live_tasks:
            state.current_task = max(
                live_tasks.values(), key=lambda t: t.started_ts,
            )
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
    """Lista issues e PRs/MRs abertos via ``gh api`` (GitHub) ou ``glab api`` (GitLab).

    Forge-aware (PR #297): quando ``forge_kind="gitlab"`` é informado, o
    provider chama ``glab api`` no endpoint REST v4 equivalente; quando
    ``"github"`` (default), mantém o comportamento legado via ``gh api``.
    O nome da classe continua ``GitHubProvider`` para compat — o renderer
    do painel não distingue PR de MR (ambos viram :class:`GitHubIssue`
    com ``is_pr=True``).
    """

    PER_PAGE = 100

    def __init__(
        self,
        repo: str = REPO_DEFAULT,
        ttl_s: float = 10.0,
        *,
        forge_kind: str = "",
    ):
        # 10s: rate-limit auth-only no GH (5000/h = 1.4/s) e GL (600/min/usuário
        # autenticado em gitlab.com). 10s/ciclo = 360/h, folga em ambos.
        self._gh = gh_bin()
        self._glab = shutil.which("glab")
        self._repo = repo
        self._forge_kind = (forge_kind or "").strip().lower()
        self._cache: Cache[GitHubSnapshot] = Cache(
            ttl_s, self._fetch, fallback=GitHubSnapshot(),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> GitHubSnapshot:
        return self._cache.get(force)

    def _fetch(self) -> GitHubSnapshot:
        # Roteia pelo forge_kind detectado — evita o pior caso anterior em que
        # o painel apontado a um NS GitLab tentava ``gh api`` num repo
        # inexistente no GitHub, esperando o timeout de 15s a cada refresh
        # (cache TTL de 10s causava ciclos consecutivos travando a UX).
        if self._forge_kind == "gitlab":
            return self._fetch_gitlab()
        return self._fetch_github()

    def _fetch_github(self) -> GitHubSnapshot:
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

    def _fetch_gitlab(self) -> GitHubSnapshot:
        """Lista issues + MRs do projeto GitLab via ``glab api``.

        Duas chamadas paralelas via subprocess síncrono: ``projects/<encoded>/
        issues?state=opened`` e ``projects/<encoded>/merge_requests?state=opened``.
        Forçamos ``-X GET`` porque ``glab api -f`` (PR #297 / E2E descobriu)
        muda o método HTTP para POST quando há parâmetros, gerando HTTP 400.
        """
        if self._glab is None:
            raise RuntimeError("glab não encontrado")
        from urllib.parse import quote as _quote
        encoded = _quote(self._repo, safe="")

        def _list(endpoint_suffix: str) -> List[Dict[str, Any]]:
            data = _capture_json(
                [self._glab, "api", "-X", "GET",
                 f"projects/{encoded}/{endpoint_suffix}",
                 "-f", "state=opened",
                 "-f", f"per_page={self.PER_PAGE}"],
                timeout=15.0,
            )
            if data is None:
                raise RuntimeError(f"glab api {endpoint_suffix} falhou")
            return data if isinstance(data, list) else []

        snap = GitHubSnapshot()
        for it in _list("issues"):
            labels = it.get("labels") or []
            if labels and isinstance(labels[0], dict):
                labels = [lbl.get("name", "") for lbl in labels]
            assignees = [a.get("username", "")
                         for a in it.get("assignees", []) or []]
            iid = int(it.get("iid") or it.get("number") or 0)
            snap.issues.append(GitHubIssue(
                number=iid, title=it.get("title", ""), is_pr=False,
                state="open", labels=list(labels), assignees=assignees,
                updated_at=_parse_k8s_ts(it.get("updated_at")),
                url=it.get("web_url", ""),
                workflow=_derive_workflow(list(labels)),
                review=_derive_review(list(labels)),
                blocked=_BLOCKED_LABEL in labels,
                refining=any(lbl == "refinar" for lbl in labels),
            ))
        for it in _list("merge_requests"):
            labels = it.get("labels") or []
            if labels and isinstance(labels[0], dict):
                labels = [lbl.get("name", "") for lbl in labels]
            assignees = [a.get("username", "")
                         for a in it.get("assignees", []) or []]
            iid = int(it.get("iid") or it.get("number") or 0)
            snap.prs.append(GitHubIssue(
                number=iid, title=it.get("title", ""), is_pr=True,
                state="open", labels=list(labels), assignees=assignees,
                updated_at=_parse_k8s_ts(it.get("updated_at")),
                url=it.get("web_url", ""),
                workflow=_derive_workflow(list(labels)),
                review=_derive_review(list(labels)),
                blocked=_BLOCKED_LABEL in labels,
                refining=any(lbl == "refinar" for lbl in labels),
            ))
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

    def get_task_cost(self, task_id: str) -> Optional[float]:
        """Total cost in USD for a specific task (session_id = ``worker_<task_id>``).

        Returns ``None`` when the DB is absent, unreadable, or has no matching
        session — the caller should treat ``None`` as "ledger unavailable".
        """
        if not self._db_path.is_file():
            return None
        session_id = f"worker_{task_id}"
        try:
            conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True, timeout=1.0,
            )
        except sqlite3.OperationalError:
            return None
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_records WHERE session_id=?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return float(row[0])
        return None


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


# ===== Tmp emptyDir resize (interativo via PodWatchView, hotkey [t]) =========
#
# Painel deixa o operador subir/baixar o ``sizeLimit`` do volume ``tmp``
# (emptyDir) do Deployment do pod sem editar YAML. Usa ``kubectl patch
# --type=strategic`` com merge-key ``name``: o K8s atualiza só a entry do
# volume "tmp", preservando os demais volumes. Como o patch substitui o
# objeto ``emptyDir`` inteiro, ``medium: Memory`` (se presente) é REMOVIDO
# — o /tmp passa a usar o disco do nó. Disparar dispara rollout automático
# (RollingUpdate ou Recreate, conforme strategy do Deployment).

_TMP_SIZE_RE = re.compile(r"^[1-9][0-9]{0,5}(Ki|Mi|Gi|Ti|Pi|Ei)$")


def set_pod_tmp_size(
    deployment: str, size: str, *, timeout: float = 15.0,
    namespace: str = NS,
) -> tuple:
    """Aplica novo ``sizeLimit`` no volume ``tmp`` (emptyDir) do Deployment.

    Strategic merge patch com merge-key ``name=tmp``: apenas o emptyDir do
    volume "tmp" é reescrito. ``medium: Memory`` é REMOVIDO (volume passa
    a usar disco do nó). Retorna ``(ok, msg)``.

    ``size`` deve casar ``_TMP_SIZE_RE`` (Ki/Mi/Gi/Ti/Pi/Ei, 1-6 dígitos).
    Validação antes de chegar em argv impede injeção em ``-p``. Audit
    emite ``AuditEvent(SECURITY_POLICY_CHANGED)`` (mesmo canal de
    ``set_preferred_model`` — operação privilegiada que muda a spec).
    """
    safe_dep = deployment if isinstance(deployment, str) else repr(deployment)
    safe_size = size if isinstance(size, str) else repr(size)
    if deployment not in _ALLOWED_DEPLOYMENTS_FULL:
        _audit_security_policy_change(
            safe_dep, safe_size,
            result="denied", detail="deployment fora da whitelist (tmp resize)",
            namespace=namespace,
        )
        allowed = ", ".join(sorted(_ALLOWED_DEPLOYMENTS_FULL))
        return False, (
            f"deployment '{deployment}' não permitido — "
            f"esperado um de: {allowed}"
        )
    if not isinstance(size, str) or not _TMP_SIZE_RE.match(size):
        _audit_security_policy_change(
            deployment, safe_size,
            result="denied", detail="size inválido (esperado: NNN[KMGTPE]i)",
            namespace=namespace,
        )
        return False, "size inválido — formato esperado: 256Mi, 1Gi, 2Gi etc"
    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_security_policy_change(
            deployment, size, result="failed", detail="kubectl não encontrado",
            namespace=namespace,
        )
        return False, "kubectl não encontrado"
    # Strategic merge: volumes list usa merge-key "name", então o K8s
    # encontra a entry "tmp" e substitui o emptyDir inteiro (sem afetar
    # outros volumes). Sem `medium` → disco do nó (não RAM).
    patch = json.dumps({
        "spec": {"template": {"spec": {"volumes": [
            {"name": "tmp", "emptyDir": {"sizeLimit": size}}
        ]}}}
    })
    _audit_security_policy_change(
        deployment, size,
        result="allowed", detail=f"executando kubectl patch (tmp={size})",
        namespace=namespace,
    )
    try:
        proc = subprocess.run(
            [kubectl, "-n", namespace, "patch", f"deploy/{deployment}",
             "--type=strategic", "-p", patch],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_security_policy_change(
            deployment, size, result="failed", detail=f"subprocess: {exc}",
            namespace=namespace,
        )
        return False, f"falha ao executar kubectl: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl patch falhou").strip()
        _audit_security_policy_change(
            deployment, size, result="failed",
            detail=f"rc={proc.returncode} {err}",
            namespace=namespace,
        )
        return False, err
    msg = (proc.stdout or "patch aplicado — rollout disparado").strip()
    _audit_security_policy_change(
        deployment, size, result="completed", detail=msg,
        namespace=namespace,
    )
    return True, msg


# ===== Pod lifecycle actions (delete / rollout / kill local) ================
#
# Operações disparadas pelo PodPickerView (hotkeys [x]/[r]/[R]). Cada uma:
#
#   1. Valida argumentos contra whitelist/regex ANTES de chegar em argv.
#   2. Emite audit `OPERATION_EXECUTED` (allowed→completed / denied / failed).
#   3. Retorna ``(ok: bool, msg: str)`` para a view renderizar feedback.
#
# A construção segue o mesmo padrão de ``set_preferred_model`` — funções puras
# testáveis sem montar UI.

def _audit_pod_action(
    action: str, resource: str, *, result: str, detail: str,
    namespace: str = NS,
) -> None:
    """Emite AuditEvent(COMMAND_EXECUTED) para ações de ciclo-de-vida.

    Espelha ``_audit_security_policy_change`` (segredos não são logados;
    apenas action/resource/result/detail). Usa COMMAND_EXECUTED por casar
    semanticamente com kubectl/os.kill — ações executando comandos
    externos contra recursos identificáveis. Silencioso quando o audit
    logger não está importável; nunca bloqueia a ação.
    """
    try:
        from deile.security.audit_logger import (  # noqa: PLC0415
            AuditEventType, SeverityLevel, get_audit_logger)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit logger indisponível para %s: %s", action, exc,
        )
        return
    severity = (SeverityLevel.INFO
                if result in ("allowed", "completed", "cancelled")
                else SeverityLevel.WARNING)
    try:
        get_audit_logger().log_event(
            event_type=AuditEventType.COMMAND_EXECUTED,
            severity=severity,
            actor=f"panel:{action}",
            resource=resource,
            action=action,
            result=result,
            details={"detail": detail[:200], "namespace": namespace},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("falha emitindo AuditEvent: %s", exc)


def delete_pod(pod_name: str, *, namespace: str = NS,
               timeout: float = 15.0) -> tuple:
    """``kubectl delete pod <name>`` com validação e audit.

    O Deployment correspondente vai recriar o Pod em segundos — equivale
    a "restart só desse pod". Para reiniciar TODOS os pods do mesmo
    Deployment, use ``rollout_restart_deployment``.
    """
    safe = pod_name if isinstance(pod_name, str) else repr(pod_name)
    resource = f"pod:{namespace}/{safe}"
    if not isinstance(pod_name, str) or not _POD_NAME_RE.match(pod_name):
        _audit_pod_action(
            "delete_pod", resource, result="denied",
            detail="pod_name inválido", namespace=namespace,
        )
        return False, "pod_name inválido — recusado por validação"
    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_pod_action(
            "delete_pod", resource, result="failed",
            detail="kubectl não encontrado", namespace=namespace,
        )
        return False, "kubectl não encontrado"
    _audit_pod_action(
        "delete_pod", resource, result="allowed",
        detail="executando kubectl delete pod", namespace=namespace,
    )
    try:
        proc = subprocess.run(
            [kubectl, "-n", namespace, "delete", "pod", pod_name],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_pod_action(
            "delete_pod", resource, result="failed",
            detail=f"subprocess: {exc}", namespace=namespace,
        )
        return False, f"falha ao executar kubectl: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl delete pod falhou").strip()
        _audit_pod_action(
            "delete_pod", resource, result="failed",
            detail=f"rc={proc.returncode} {err}", namespace=namespace,
        )
        return False, err
    msg = (proc.stdout or "pod deletado").strip()
    _audit_pod_action(
        "delete_pod", resource, result="completed",
        detail=msg, namespace=namespace,
    )
    return True, msg


def rollout_restart_deployment(deployment: str, *,
                               namespace: str = NS,
                               timeout: float = 15.0) -> tuple:
    """``kubectl rollout restart deployment/<name>`` com whitelist + audit.

    Reinicia TODOS os pods do Deployment (respeitando a strategy do
    manifest — RollingUpdate ou Recreate). Whitelist restrita aos 4
    deployments do stack (``_ALLOWED_DEPLOYMENTS_FULL``) bloqueia qualquer
    alvo fora dali.
    """
    safe = deployment if isinstance(deployment, str) else repr(deployment)
    resource = f"deployment:{namespace}/{safe}"
    if deployment not in _ALLOWED_DEPLOYMENTS_FULL:
        _audit_pod_action(
            "rollout_restart", resource, result="denied",
            detail="deployment fora da whitelist", namespace=namespace,
        )
        allowed = ", ".join(sorted(_ALLOWED_DEPLOYMENTS_FULL))
        return False, (
            f"deployment '{deployment}' não permitido — "
            f"esperado um de: {allowed}"
        )
    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_pod_action(
            "rollout_restart", resource, result="failed",
            detail="kubectl não encontrado", namespace=namespace,
        )
        return False, "kubectl não encontrado"
    _audit_pod_action(
        "rollout_restart", resource, result="allowed",
        detail="executando kubectl rollout restart", namespace=namespace,
    )
    try:
        proc = subprocess.run(
            [kubectl, "-n", namespace, "rollout", "restart",
             f"deployment/{deployment}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_pod_action(
            "rollout_restart", resource, result="failed",
            detail=f"subprocess: {exc}", namespace=namespace,
        )
        return False, f"falha ao executar kubectl: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "rollout restart falhou").strip()
        _audit_pod_action(
            "rollout_restart", resource, result="failed",
            detail=f"rc={proc.returncode} {err}", namespace=namespace,
        )
        return False, err
    msg = (proc.stdout or "rollout disparado").strip()
    _audit_pod_action(
        "rollout_restart", resource, result="completed",
        detail=msg, namespace=namespace,
    )
    return True, msg


def rollout_restart_all(*, namespace: str = NS,
                        timeout: float = 15.0) -> List[tuple]:
    """Dispara ``rollout restart`` para CADA deployment k8s do stack.

    Best-effort por deployment — uma falha NÃO aborta as demais (cada
    rollout é independente; é melhor reiniciar 3 de 4 do que 0). Retorna
    a lista `[(deployment, ok, msg), ...]` na ordem da whitelist
    determinística (sort), para o caller agregar e exibir.
    """
    out: List[tuple] = []
    for dep in sorted(_ALLOWED_DEPLOYMENTS_FULL):
        ok, msg = rollout_restart_deployment(
            dep, namespace=namespace, timeout=timeout,
        )
        out.append((dep, ok, msg))
    return out


def kill_local_pid(pid: int, *, sig: str = "SIGTERM",
                   timeout: float = 5.0) -> tuple:
    """Envia ``sig`` (SIGTERM por default) para o processo local ``pid``.

    Validações:

    1. PID positivo (> 1 — proíbe 0, init, negativos).
    2. PID pertence ao mesmo USER que roda o painel — nunca toca processo
       alheio (defesa em profundidade; se o painel rodar privilegiado por
       acidente, ainda restringe ao escopo natural do operador).
    3. ``sig`` numa whitelist pequena (SIGTERM / SIGKILL) — evita usar
       sinais raros (SIGUSR1, etc) que poderiam corromper estado.

    Se o processo não morrer em ``timeout`` segundos após SIGTERM, faz
    SIGKILL como segundo passo (mesmo padrão que ``_LogStreamer.stop``).
    Retorna ``(ok, msg)`` — view só renderiza.
    """
    import os
    import signal as _signal
    resource = f"local-pid:{pid}"
    allowed_signals = {"SIGTERM": _signal.SIGTERM, "SIGKILL": _signal.SIGKILL}
    if sig not in allowed_signals:
        _audit_pod_action(
            "kill_local_pid", resource, result="denied",
            detail=f"sig '{sig}' fora da whitelist",
        )
        return False, f"sinal '{sig}' não permitido"
    if not isinstance(pid, int) or pid <= 1:
        _audit_pod_action(
            "kill_local_pid", resource, result="denied",
            detail=f"pid inválido ({pid!r})",
        )
        return False, f"pid inválido: {pid!r}"
    # Ownership check (defense in depth). psutil.AccessDenied no fetch
    # do uid é tratado como "não posso ver → não posso matar".
    try:
        import psutil  # noqa: PLC0415
        proc = psutil.Process(pid)
        proc_uid = proc.uids().real
        my_uid = os.getuid()
        if proc_uid != my_uid:
            _audit_pod_action(
                "kill_local_pid", resource, result="denied",
                detail=f"pid pertence a uid={proc_uid}, painel é uid={my_uid}",
            )
            return False, (
                f"pid {pid} pertence a outro usuário (uid={proc_uid})"
            )
    except psutil.NoSuchProcess:
        _audit_pod_action(
            "kill_local_pid", resource, result="failed",
            detail="processo não existe (já morreu?)",
        )
        return False, f"processo {pid} não existe"
    except psutil.AccessDenied as exc:
        _audit_pod_action(
            "kill_local_pid", resource, result="denied",
            detail=f"psutil acesso negado: {exc}",
        )
        return False, f"acesso negado ao pid {pid}"
    except Exception as exc:  # noqa: BLE001
        _audit_pod_action(
            "kill_local_pid", resource, result="failed",
            detail=f"psutil falhou: {exc}",
        )
        return False, f"erro inspecionando pid {pid}: {exc}"
    _audit_pod_action(
        "kill_local_pid", resource, result="allowed",
        detail=f"enviando {sig} (escalation: SIGKILL após {timeout}s)",
    )
    try:
        os.kill(pid, allowed_signals[sig])
    except ProcessLookupError:
        _audit_pod_action(
            "kill_local_pid", resource, result="failed",
            detail="ProcessLookupError no os.kill",
        )
        return False, "processo desapareceu antes do sinal chegar"
    except PermissionError as exc:
        _audit_pod_action(
            "kill_local_pid", resource, result="failed",
            detail=f"PermissionError: {exc}",
        )
        return False, f"sem permissão para matar pid {pid}"
    # Espera o processo morrer; se não morrer em ``timeout``, escala SIGKILL.
    if sig == "SIGTERM":
        try:
            proc.wait(timeout=timeout)
            msg = f"pid {pid} encerrado via SIGTERM"
        except psutil.TimeoutExpired:
            try:
                os.kill(pid, _signal.SIGKILL)
                msg = f"pid {pid} forçado via SIGKILL (SIGTERM ignorado)"
            except ProcessLookupError:
                msg = f"pid {pid} morreu durante escalation"
            except PermissionError as exc:
                _audit_pod_action(
                    "kill_local_pid", resource, result="failed",
                    detail=f"escalation negada: {exc}",
                )
                return False, f"sem permissão na escalation: {exc}"
    else:
        msg = f"pid {pid} encerrado via {sig}"
    _audit_pod_action(
        "kill_local_pid", resource, result="completed", detail=msg,
    )
    return True, msg


# ===== Per-stage model override (issue #305) ================================
#
# Key architectural note: the panel runs on the OPERATOR's host (Mac/laptop),
# but the per-stage model setting needs to take effect inside the
# ``deile-worker`` Pod. Writing to the operator's local ``~/.deile/settings.json``
# has ZERO effect on the cluster (the pod has its own filesystem).
#
# So the cluster-side write path mirrors ``set_preferred_model``:
# ``kubectl set env deploy/deile-worker DEILE_PIPELINE_MODEL_<STAGE>=<slug>``.
# The worker reads ``DEILE_PIPELINE_MODEL_<STAGE>`` env vars at startup via
# ``_apply_env_overrides`` in ``deile/config/settings.py`` (issue #305), so a
# RollingUpdate after the set-env applies the new value pod-wide.
#
# The settings.json path (``pipeline.models.<stage>``) is still wired in
# Settings — it's the local-CLI path (operator running DEILE on Mac, not in
# cluster). The panel just doesn't use it because it doesn't reach the pod.

# The 5 stage env var slots (must mirror PIPELINE_STAGES + the _ENV_OVERRIDES
# table in deile/config/settings.py). Kept as a module-level tuple so the
# provider/setter/HelpView all agree on the wire format.
_STAGE_ENV_VARS: tuple = (
    ("classify",    "DEILE_PIPELINE_MODEL_CLASSIFY"),
    ("refine",      "DEILE_PIPELINE_MODEL_REFINE"),
    ("implement",   "DEILE_PIPELINE_MODEL_IMPLEMENT"),
    ("pr_review",   "DEILE_PIPELINE_MODEL_PR_REVIEW"),
    ("follow_ups",  "DEILE_PIPELINE_MODEL_FOLLOW_UPS"),
)
#: The DEPLOYMENT that consumes the ``DEILE_PIPELINE_MODEL_<STAGE>`` env vars.
#: MUST be ``deile-pipeline`` — it is the pod that calls ``resolve_stage_model()``
#: (in ``deile/orchestration/pipeline/model_resolver.py``) and injects the
#: result into ``DispatchPayload.preferred_model`` sent to claude-worker /
#: deile-worker. Writing these env vars to ``deile-worker`` instead is silent
#: cost amplifier: the override is invisible to the resolver, so the worker /
#: claude-worker fall back to OAuth default (Opus 4.7 = $5/$25 per 1M tokens).
#: Bug found 2026-05-27 — user configured sonnet-4-6 in the panel and got Opus.
_STAGE_DEPLOYMENT = "deile-pipeline"


@dataclass(frozen=True)
class StageModelEntry:
    """One row in the per-stage model override view.

    - ``stage`` — canonical stage name (one of ``PIPELINE_STAGES``).
    - ``override`` — value of ``DEILE_PIPELINE_MODEL_<STAGE>`` env var on the
      ``deile-worker`` Deployment; ``None`` when no per-stage override is set.
    - ``effective`` — what the worker will actually use for this stage on
      the next dispatch: the override, or the worker's
      ``DEILE_PREFERRED_MODEL`` (read via :class:`CurrentModelProvider`).
    - ``is_fallback`` — True iff ``effective`` came from the global default,
      not the per-stage override. The view marks fallback rows with ``◄``.
    """

    stage: str
    override: Optional[str]
    effective: Optional[str]
    is_fallback: bool


class StageModelsProvider(_KubectlProviderMixin):
    """Per-stage model overrides + global default, read from the cluster.

    Mirrors :class:`CurrentModelProvider` (single ``kubectl get -o json`` of
    the ``deile-worker`` Deployment) but extracts the 5
    ``DEILE_PIPELINE_MODEL_<STAGE>`` env vars instead of
    ``DEILE_PREFERRED_MODEL``. TTL aligned with ``CurrentModelProvider`` (3s)
    so a single ``[r]`` refresh sweeps both.

    The provider does NOT read the operator's local ``Settings`` singleton —
    that would be misleading, because the operator's local
    ``~/.deile/settings.json`` does not propagate to the worker pod.
    """

    def __init__(self, ttl_s: float = 3.0, enabled: bool = True):
        self._kubectl = kubectl_bin()
        self._enabled = enabled
        self._cache: Cache[List[StageModelEntry]] = Cache(
            ttl_s, self._fetch, fallback=[],
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> List[StageModelEntry]:
        return self._cache.get(force)

    def _fetch(self) -> List[StageModelEntry]:
        # `--local-only` → cai no fallback do Cache sem chamar kubectl.
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        data = _capture_json(
            [self._kubectl, "-n", NS, "get",
             f"deployment/{_STAGE_DEPLOYMENT}", "-o", "json"],
            timeout=4.0,
        )
        if not data:
            raise RuntimeError(
                f"kubectl get deployment/{_STAGE_DEPLOYMENT} falhou ou vazio"
            )
        env_map = self._extract_env_map(data)
        global_default = env_map.get("DEILE_PREFERRED_MODEL") or None
        out: List[StageModelEntry] = []
        for stage, env_var in _STAGE_ENV_VARS:
            override = env_map.get(env_var) or None
            effective = override or global_default
            out.append(StageModelEntry(
                stage=stage,
                override=override,
                effective=effective,
                is_fallback=(override is None and effective is not None),
            ))
        return out

    @staticmethod
    def _extract_env_map(data: Dict[str, Any]) -> Dict[str, str]:
        """Return ``{env_name: env_value}`` for the first container's env list.

        Matches CurrentModelProvider's extraction shape; centralised so the
        same parsing logic doesn't drift between the two providers.
        """
        containers = (data.get("spec", {}).get("template", {})
                      .get("spec", {}).get("containers", []) or [])
        if not containers:
            return {}
        out: Dict[str, str] = {}
        for env in (containers[0].get("env") or []):
            name = env.get("name")
            value = env.get("value")
            if name and isinstance(value, str):
                out[name] = value
        return out


def _audit_stage_model_change(
    stage: str, slug: Optional[str], *, result: str, detail: str,
) -> None:
    """Emit AuditEvent(SECURITY_POLICY_CHANGED) for per-stage model writes.

    Same envelope as :func:`_audit_security_policy_change` (parity with the
    deployment-wide ``set_preferred_model``) so dashboards can grep for both
    under the same event type. ``slug=None`` is the clear/reset variant.
    """
    try:
        from deile.security.audit_logger import (  # noqa: PLC0415
            AuditEventType, SeverityLevel, get_audit_logger)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit logger indisponível para set_stage_model: %s", exc,
        )
        return
    severity = (SeverityLevel.INFO
                if result in ("allowed", "completed", "cancelled")
                else SeverityLevel.WARNING)
    env_var = next((e for s, e in _STAGE_ENV_VARS if s == stage),
                   f"DEILE_PIPELINE_MODEL_{stage.upper()}")
    try:
        get_audit_logger().log_event(
            event_type=AuditEventType.SECURITY_POLICY_CHANGED,
            severity=severity,
            actor="panel:set_stage_model",
            resource=f"deployment:{NS}/{_STAGE_DEPLOYMENT}:{env_var}",
            action="kubectl_set_env" if slug else "kubectl_unset_env",
            result=result,
            details={"stage": stage, "slug": slug or "", "detail": detail[:200]},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("falha emitindo AuditEvent (stage_model): %s", exc)


def _env_var_for_stage(stage: str) -> Optional[str]:
    """Return ``DEILE_PIPELINE_MODEL_<STAGE>`` for a canonical stage, else None.

    Single source of truth for the stage→env mapping; rejects unknown stages.
    """
    return next((env for s, env in _STAGE_ENV_VARS if s == stage), None)


def set_stage_model(stage: str, slug: str, timeout: float = 15.0) -> tuple:
    """Pin a per-stage model override on ``deile-worker`` (issue #305).

    Uses ``kubectl set env`` to write ``DEILE_PIPELINE_MODEL_<STAGE>=<slug>``
    on the ``deile-worker`` Deployment. The worker reads
    ``DEILE_PIPELINE_MODEL_<STAGE>`` at startup (registered in
    ``_ENV_OVERRIDES`` of ``deile/config/settings.py``), and the RollingUpdate
    strategy applies the new value zero-downtime. Returns ``(ok, msg)``.

    Slug is validated against ``_MODEL_SLUG_RE`` BEFORE reaching kubectl argv
    — same defense as :func:`set_preferred_model`. Emits a
    ``SECURITY_POLICY_CHANGED`` audit event on every attempt (allowed /
    denied / completed / failed), under the same event type as
    ``set_preferred_model`` so dashboards see them uniformly.
    """
    safe_slug = slug if isinstance(slug, str) else repr(slug)
    env_var = _env_var_for_stage(stage)
    if env_var is None:
        _audit_stage_model_change(
            str(stage), safe_slug, result="denied",
            detail="stage fora do conjunto canônico",
        )
        allowed = ", ".join(s for s, _ in _STAGE_ENV_VARS)
        return False, f"stage '{stage}' inválido — esperado um de: {allowed}"
    if not isinstance(slug, str) or not _MODEL_SLUG_RE.match(slug):
        _audit_stage_model_change(
            stage, safe_slug, result="denied", detail="slug inválido",
        )
        return False, "slug inválido — recusado por validação"
    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_stage_model_change(
            stage, slug, result="failed", detail="kubectl não encontrado",
        )
        return False, "kubectl não encontrado"
    _audit_stage_model_change(
        stage, slug, result="allowed",
        detail=f"executando kubectl set env {env_var}",
    )
    try:
        proc = subprocess.run(
            [kubectl, "-n", NS, "set", "env", f"deploy/{_STAGE_DEPLOYMENT}",
             f"{env_var}={slug}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_stage_model_change(
            stage, slug, result="failed", detail=f"subprocess: {exc}",
        )
        return False, f"falha ao executar kubectl: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        _audit_stage_model_change(
            stage, slug, result="failed",
            detail=f"rc={proc.returncode} {err}",
        )
        return False, err
    msg = (proc.stdout or "rollout disparado").strip()
    _audit_stage_model_change(
        stage, slug, result="completed", detail=msg,
    )
    return True, f"{env_var}={slug} ({msg})"


def clear_stage_model(stage: str, timeout: float = 15.0) -> tuple:
    """Remove a per-stage override on ``deile-worker``. Returns ``(ok, msg)``.

    Uses ``kubectl set env ... <VAR>-`` (the trailing dash is kubectl's
    syntax for "unset"). After the rollout, the worker reads no
    ``DEILE_PIPELINE_MODEL_<STAGE>`` env, so :func:`resolve_stage_model`
    returns ``None`` and the dispatch falls back to the global default.
    """
    env_var = _env_var_for_stage(stage)
    if env_var is None:
        _audit_stage_model_change(
            str(stage), None, result="denied",
            detail="stage fora do conjunto canônico",
        )
        return False, f"stage '{stage}' inválido"
    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_stage_model_change(
            stage, None, result="failed", detail="kubectl não encontrado",
        )
        return False, "kubectl não encontrado"
    _audit_stage_model_change(
        stage, None, result="allowed",
        detail=f"executando kubectl set env {env_var}-",
    )
    try:
        proc = subprocess.run(
            [kubectl, "-n", NS, "set", "env", f"deploy/{_STAGE_DEPLOYMENT}",
             f"{env_var}-"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_stage_model_change(
            stage, None, result="failed", detail=f"subprocess: {exc}",
        )
        return False, f"falha ao executar kubectl: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        _audit_stage_model_change(
            stage, None, result="failed",
            detail=f"rc={proc.returncode} {err}",
        )
        return False, err
    msg = (proc.stdout or "rollout disparado").strip()
    _audit_stage_model_change(
        stage, None, result="completed", detail=msg,
    )
    return True, f"{env_var} unset ({msg})"


# ===== Pipeline dispatch mode (issue #309) ==================================
#
# O ``PipelineMonitor`` lê ``settings.pipeline_dispatch_mode`` no boot e instancia
# ``ClaudeImplementer`` (``claude -p`` num worktree local) ou ``WorkerImplementer``
# (HTTP → deile-worker). O ConfigMap ``deile-runtime-config`` traz o default
# (``deile_worker``), mas o painel TUI permite flipar o modo sem editar manifest
# nem ConfigMap: ``kubectl set env deploy/deile-pipeline DEILE_PIPELINE_DISPATCH_MODE=<mode>``.
#
# A env var é tecnicamente *deprecated* a favor de ``pipeline.dispatch_mode`` no
# settings.json (issue #111), mas é o único caminho que NÃO exige editar +
# re-aplicar ConfigMap (que não propaga live em subPath mounts) — e o
# ``kubectl set env`` dispara rollout, então a config nova chega no pod novo
# como qualquer outro `kubectl set env` da casa. Continuamos compat com a env
# var; quem prefere editar JSON pode editar o ConfigMap manualmente.
#
# **Limitação operacional documentada**: setar ``claude`` aqui só funciona se o
# binary ``claude`` estiver no PATH dentro do pod *e* houver credentials para
# ``~/.claude/`` (subscription ou API key). Hoje o image NÃO instala o
# ``claude`` CLI nem monta credentials — follow-up explícito no body do PR
# de #309 (e a próxima dispatch ``claude`` no pipeline emite warning claro de
# ``claude binary not found in PATH``).
_DISPATCH_MODES_ALLOWED: tuple = ("claude", "deile_worker")
_DISPATCH_DEPLOYMENT = "deile-pipeline"
_DISPATCH_ENV_VAR = "DEILE_PIPELINE_DISPATCH_MODE"


@dataclass(frozen=True)
class DispatchModeEntry:
    """Snapshot do dispatch mode lido do cluster (issue #309).

    - ``mode`` — valor corrente de ``DEILE_PIPELINE_DISPATCH_MODE`` no
      Deployment ``deile-pipeline``. ``None`` quando a env var não está setada
      (o pod cai no default carregado do settings.json — ``deile_worker``).
    - ``source`` — ``"env"`` quando lida da Deployment spec; ``"default"``
      quando não há env e o painel infere o valor declarado no ConfigMap.
    - ``effective`` — o que o pod realmente vai usar na próxima dispatch:
      ``mode`` quando presente, senão o default do settings.json layered.
    """

    mode: Optional[str]
    source: str
    effective: str


class DispatchModeProvider(_KubectlProviderMixin):
    """Lê o dispatch mode corrente da Deployment ``deile-pipeline``.

    Mirrors :class:`CurrentModelProvider` (um único ``kubectl get -o json``)
    mas extrai ``DEILE_PIPELINE_DISPATCH_MODE`` em vez de
    ``DEILE_PREFERRED_MODEL``. TTL alinhado com ``CurrentModelProvider`` (3s).

    Multi-NS (PR #315): aceita ``namespace=`` no construtor para casar com o
    namespace efetivo do painel. Quando o operador escolhe NS via menu, o
    provider precisa ler do mesmo NS que :func:`set_pipeline_dispatch_mode`
    escreve — senão a leitura mostra estado de outro cluster.
    """

    # Default declarado no ConfigMap ``deile-runtime-config`` (manifest 47).
    # **Drift risk**: se o ConfigMap mudar (ex.: novo default ``claude``), este
    # valor precisa ser bumpado em sincronia. Hoje não há leitor de ConfigMap
    # aqui — o painel não monta o ConfigMap no host. Aceito como pequena
    # constante de espelhamento; alternativa (ler via ``kubectl get cm
    # deile-runtime-config -o jsonpath=…``) acrescenta um subprocess por
    # refresh e foi rejeitada pelo custo.
    _DEFAULT_FROM_CONFIGMAP = "deile_worker"

    def __init__(self, ttl_s: float = 3.0, enabled: bool = True,
                 namespace: str = NS):
        self._kubectl = kubectl_bin()
        self._enabled = enabled
        self._namespace = namespace
        self._cache: Cache[DispatchModeEntry] = Cache(
            ttl_s, self._fetch,
            fallback=DispatchModeEntry(
                mode=None, source="default",
                effective=self._DEFAULT_FROM_CONFIGMAP,
            ),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> DispatchModeEntry:
        return self._cache.get(force)

    def _fetch(self) -> DispatchModeEntry:
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        data = _capture_json(
            [self._kubectl, "-n", self._namespace, "get",
             f"deployment/{_DISPATCH_DEPLOYMENT}", "-o", "json"],
            timeout=4.0,
        )
        if not data:
            raise RuntimeError(
                f"kubectl get deployment/{_DISPATCH_DEPLOYMENT} falhou ou vazio"
            )
        containers = (data.get("spec", {}).get("template", {})
                      .get("spec", {}).get("containers", []) or [])
        env_value: Optional[str] = None
        envs = (containers[0].get("env") or []) if containers else []
        for env in envs:
            if env.get("name") == _DISPATCH_ENV_VAR:
                raw = env.get("value")
                if isinstance(raw, str) and raw.strip():
                    # Canonicaliza aliases (``worker``, ``claude_code``...) para
                    # o conjunto whitelist da UI. Se alguém setou manualmente
                    # ``DEILE_PIPELINE_DISPATCH_MODE=worker`` (alias válido para
                    # ``build_implementer``), o painel mostra ``deile_worker``
                    # — alinhado com a opção que aparece no picker, em vez de
                    # exibir um valor que não destaca nenhuma linha.
                    env_value = _canonicalize_dispatch_alias(raw)
                break
        if env_value:
            return DispatchModeEntry(
                mode=env_value, source="env", effective=env_value,
            )
        return DispatchModeEntry(
            mode=None, source="default",
            effective=self._DEFAULT_FROM_CONFIGMAP,
        )


def _canonicalize_dispatch_alias(raw: str) -> str:
    """Normaliza aliases de dispatch_mode para o conjunto canônico da UI.

    ``build_implementer`` aceita aliases (``worker``, ``deile-worker``,
    ``claude_code``...), mas o painel mostra só o conjunto canônico
    (``claude`` | ``deile_worker``). Esse helper traduz: alias conhecido →
    forma canônica; valor desconhecido → devolvido como-veio em lowercase
    (não esconde valor estranho na UI; o operador vê o que está no cluster).
    """
    # Import local NÃO por dependência circular (infra/k8s/ é leaf no grafo
    # de imports do DEILE) mas por custo de cold-import: puxar o módulo
    # ``implementer`` inteiro só pra ler duas frozensets adiciona ~500ms ao
    # boot do painel. Deferir o import para o primeiro `kubectl get` (que já
    # paga o custo do subprocess) deixa o painel responsivo na abertura.
    from deile.orchestration.pipeline.implementer import (  # noqa: PLC0415
        CLAUDE_ALIASES, WORKER_ALIASES)
    lo = raw.strip().lower()
    if lo in WORKER_ALIASES:
        return "deile_worker"
    if lo in CLAUDE_ALIASES:
        return "claude"
    return lo


def _audit_dispatch_mode_change(
    mode: Optional[str], *, result: str, detail: str,
    namespace: str = NS,
) -> None:
    """Audit ``SECURITY_POLICY_CHANGED`` para troca de dispatch mode (#309).

    Mesma envelope de :func:`_audit_security_policy_change` (preferred_model)
    e :func:`_audit_stage_model_change` (stage models) para que dashboards
    grepem os três sob o mesmo event type. ``mode=None`` é a variante
    clear/reset (volta ao default do settings.json).
    """
    try:
        from deile.security.audit_logger import (  # noqa: PLC0415
            AuditEventType, SeverityLevel, get_audit_logger)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit logger indisponível para set_pipeline_dispatch_mode: %s",
            exc,
        )
        return
    severity = (SeverityLevel.INFO
                if result in ("allowed", "completed", "cancelled")
                else SeverityLevel.WARNING)
    try:
        get_audit_logger().log_event(
            event_type=AuditEventType.SECURITY_POLICY_CHANGED,
            severity=severity,
            actor="panel:set_pipeline_dispatch_mode",
            resource=f"deployment:{namespace}/{_DISPATCH_DEPLOYMENT}:{_DISPATCH_ENV_VAR}",
            action="kubectl_set_env" if mode else "kubectl_unset_env",
            result=result,
            # ``mode`` é ``None`` em clear/cancel-without-mode paths — o JSON
            # canônico do envelope mantém ``None`` em vez de coage para ``""``,
            # pra log analysis distinguir "mode vazio (set degenerado)" de
            # "modo absent (clear/cancel-of-clear)".
            details={"mode": mode, "detail": detail[:200]},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("falha emitindo AuditEvent (dispatch_mode): %s", exc)


def set_pipeline_dispatch_mode(
    mode: str, *, namespace: str = NS, timeout: float = 15.0,
) -> tuple:
    """Pin ``DEILE_PIPELINE_DISPATCH_MODE=<mode>`` em ``deile-pipeline`` (#309).

    Usa ``kubectl set env deploy/deile-pipeline`` e dispara o rollout
    (strategy ``Recreate`` do pipeline). Returns ``(ok, msg)``.

    O argumento ``mode`` é validado contra ``_DISPATCH_MODES_ALLOWED`` ANTES
    de virar argv — rejeita typos (``claude_p``, ``deile-worker``...) que de
    outra forma cairiam silenciosamente no fallback do ``build_implementer``
    e quebrariam a próxima dispatch.
    """
    safe_mode = mode if isinstance(mode, str) else repr(mode)
    if not isinstance(mode, str) or mode.strip().lower() not in _DISPATCH_MODES_ALLOWED:
        _audit_dispatch_mode_change(
            safe_mode, result="denied",
            detail="dispatch_mode fora do conjunto canônico",
            namespace=namespace,
        )
        allowed = ", ".join(sorted(_DISPATCH_MODES_ALLOWED))
        return False, (
            f"dispatch_mode '{mode}' inválido — "
            f"esperado um de: {allowed}"
        )
    canonical = mode.strip().lower()
    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_dispatch_mode_change(
            canonical, result="failed", detail="kubectl não encontrado",
            namespace=namespace,
        )
        return False, "kubectl não encontrado"
    _audit_dispatch_mode_change(
        canonical, result="allowed",
        detail=f"executando kubectl set env {_DISPATCH_ENV_VAR}={canonical}",
        namespace=namespace,
    )
    try:
        proc = subprocess.run(
            [kubectl, "-n", namespace, "set", "env",
             f"deploy/{_DISPATCH_DEPLOYMENT}",
             f"{_DISPATCH_ENV_VAR}={canonical}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_dispatch_mode_change(
            canonical, result="failed", detail=f"subprocess: {exc}",
            namespace=namespace,
        )
        return False, f"falha ao executar kubectl: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        _audit_dispatch_mode_change(
            canonical, result="failed",
            detail=f"rc={proc.returncode} {err}", namespace=namespace,
        )
        return False, err
    msg = (proc.stdout or "rollout disparado").strip()
    _audit_dispatch_mode_change(
        canonical, result="completed", detail=msg, namespace=namespace,
    )
    return True, f"{_DISPATCH_ENV_VAR}={canonical} ({msg})"


def clear_pipeline_dispatch_mode(
    *, namespace: str = NS, timeout: float = 15.0,
) -> tuple:
    """Remove o override de dispatch mode no ``deile-pipeline`` (#309).

    Usa ``kubectl set env ... <VAR>-`` (trailing dash = unset). Depois do
    rollout, o pod relê o settings.json layered e cai no default declarado
    no ConfigMap ``deile-runtime-config`` (``deile_worker``).
    """
    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_dispatch_mode_change(
            None, result="failed", detail="kubectl não encontrado",
            namespace=namespace,
        )
        return False, "kubectl não encontrado"
    _audit_dispatch_mode_change(
        None, result="allowed",
        detail=f"executando kubectl set env {_DISPATCH_ENV_VAR}-",
        namespace=namespace,
    )
    try:
        proc = subprocess.run(
            [kubectl, "-n", namespace, "set", "env",
             f"deploy/{_DISPATCH_DEPLOYMENT}",
             f"{_DISPATCH_ENV_VAR}-"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_dispatch_mode_change(
            None, result="failed", detail=f"subprocess: {exc}",
            namespace=namespace,
        )
        return False, f"falha ao executar kubectl: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        _audit_dispatch_mode_change(
            None, result="failed",
            detail=f"rc={proc.returncode} {err}", namespace=namespace,
        )
        return False, err
    msg = (proc.stdout or "rollout disparado").strip()
    _audit_dispatch_mode_change(
        None, result="completed", detail=msg, namespace=namespace,
    )
    return True, f"{_DISPATCH_ENV_VAR} unset ({msg})"


# ===== Per-stage dispatch override (issue #309 fase 2 — Task 19) ===========
#
# ``set_pipeline_dispatch_stage`` espelha :func:`set_pipeline_dispatch_mode`
# (global flip da PR #330) para a chain per-stage da issue #309 fase 2. Escreve
# ``DEILE_PIPELINE_DISPATCH_<STAGE>=<dispatcher>`` no Deployment
# ``deile-pipeline`` via ``kubectl set env``. O resolver (:func:`resolve_stage_dispatcher`
# em ``deile.orchestration.pipeline.dispatch_resolver``) prefere essa env var
# por-stage sobre o global ``DEILE_PIPELINE_DISPATCH_MODE``.
#
# Validação por duas camadas:
#   1. *stage* deve estar em :data:`PIPELINE_STAGES` — rejeita typo antes do
#      argv para nunca escrever ``DEILE_PIPELINE_DISPATCH_GARBAGE=...`` no pod.
#   2. *dispatcher* (quando não-None) deve passar por
#      :func:`is_valid_dispatcher` — aceita aliases legacy (``deile_worker``,
#      ``claude``, ``worker``) E a forma canônica (``deile-worker`` |
#      ``claude-worker``). Sem isso, escrever um typo (``claud-worker``) faria
#      a próxima dispatch cair silenciosamente em ``deile-worker`` por
#      fail-open do _canonicalize do resolver.
#
# ``dispatcher=None`` é o caminho de clear/reset: kubectl ``VAR-`` (com hífen
# final), idêntico a :func:`clear_pipeline_dispatch_mode` e :func:`clear_stage_model`.
def _audit_dispatch_stage_change(
    stage: str, dispatcher: Optional[str], *, result: str, detail: str,
    namespace: str = NS,
) -> None:
    """Audit ``SECURITY_POLICY_CHANGED`` para troca de dispatcher per-stage.

    Espelha :func:`_audit_dispatch_mode_change` (global flip) e
    :func:`_audit_stage_model_change` (per-stage model) — mesmo event type
    para que dashboards grepem os três sob a mesma envelope. ``dispatcher=None``
    é a variante clear/reset (volta ao fallback global / built-in).
    """
    try:
        from deile.security.audit_logger import (  # noqa: PLC0415
            AuditEventType, SeverityLevel, get_audit_logger)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit logger indisponível para set_pipeline_dispatch_stage: %s",
            exc,
        )
        return
    severity = (SeverityLevel.INFO
                if result in ("allowed", "completed", "cancelled")
                else SeverityLevel.WARNING)
    env_var = f"DEILE_PIPELINE_DISPATCH_{stage.upper()}"
    try:
        get_audit_logger().log_event(
            event_type=AuditEventType.SECURITY_POLICY_CHANGED,
            severity=severity,
            actor="panel:set_pipeline_dispatch_stage",
            resource=f"deployment:{namespace}/{_DISPATCH_DEPLOYMENT}:{env_var}",
            action="kubectl_set_env" if dispatcher else "kubectl_unset_env",
            result=result,
            details={
                "stage": stage,
                # ``dispatcher`` mantém ``None`` no envelope canônico (clear /
                # cancel-of-clear paths) para o log analysis poder distinguir
                # "dispatcher vazio (set degenerado)" de "dispatcher ausente".
                "dispatcher": dispatcher,
                "detail": detail[:200],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "falha emitindo AuditEvent (dispatch_stage): %s", exc,
        )


def set_pipeline_dispatch_stage(
    stage: str, dispatcher: Optional[str], *,
    namespace: str = NS, timeout: float = 15.0,
) -> tuple:
    """Pin ``DEILE_PIPELINE_DISPATCH_<STAGE>=<dispatcher>`` em ``deile-pipeline``.

    Usa ``kubectl set env deploy/deile-pipeline`` e dispara o rollout (strategy
    ``Recreate`` do pipeline). Returns ``(ok, msg)``.

    Args:
        stage: nome canônico do stage (um de :data:`PIPELINE_STAGES`).
            Validado ANTES de virar argv.
        dispatcher: ``"deile-worker"`` | ``"claude-worker"`` | alias legacy
            aceito por :func:`is_valid_dispatcher`. ``None`` = clear (kubectl
            ``VAR-``); o pipeline volta a usar a chain global +
            built-in default.
        namespace: K8s namespace alvo (multi-NS / PR #315).
        timeout: timeout do ``kubectl set env``.

    Emite ``SECURITY_POLICY_CHANGED`` em todos os outcomes (allowed/denied/
    completed/failed) com a mesma envelope de :func:`set_pipeline_dispatch_mode`.
    """
    # --- Validação 1: stage canônico. Lazy import de PIPELINE_STAGES +
    # is_valid_dispatcher para não puxar deile.orchestration no cold-import.
    try:
        from deile.orchestration.pipeline.dispatch_resolver import (  # noqa: PLC0415
            PIPELINE_STAGES, is_valid_dispatcher)
    except ImportError as exc:
        # Fallback hardcoded — só ocorre se dispatch_resolver tiver sido
        # removido (programming bug). O painel tem que devolver erro claro
        # em vez de levantar (UI quebra silenciosa é o pior caso).
        _audit_dispatch_stage_change(
            str(stage), dispatcher, result="failed",
            detail=f"dispatch_resolver import falhou: {exc}",
            namespace=namespace,
        )
        return False, f"dispatch_resolver indisponível: {exc}"

    if stage not in PIPELINE_STAGES:
        msg_detail = "stage fora do conjunto canônico"
        _audit_dispatch_stage_change(
            str(stage), dispatcher, result="denied", detail=msg_detail,
            namespace=namespace,
        )
        allowed = ", ".join(PIPELINE_STAGES)
        return False, (
            f"invalid stage {stage!r} — esperado um de: {allowed}"
        )

    # --- Validação 2: dispatcher (quando não-None). ``None`` = clear path.
    if dispatcher is not None and not is_valid_dispatcher(dispatcher):
        _audit_dispatch_stage_change(
            stage, dispatcher, result="denied",
            detail="dispatcher fora do conjunto whitelisted",
            namespace=namespace,
        )
        return False, (
            f"invalid dispatcher {dispatcher!r} — esperado canônico "
            f"'deile-worker'/'claude-worker' ou alias"
        )

    env_var = f"DEILE_PIPELINE_DISPATCH_{stage.upper()}"

    kubectl = kubectl_bin()
    if kubectl is None:
        _audit_dispatch_stage_change(
            stage, dispatcher, result="failed", detail="kubectl não encontrado",
            namespace=namespace,
        )
        return False, "kubectl não encontrado"

    # --- argv: set ou clear (trailing dash). Espelha set_stage_model /
    # clear_pipeline_dispatch_mode.
    if dispatcher is None:
        argv = [kubectl, "-n", namespace, "set", "env",
                f"deploy/{_DISPATCH_DEPLOYMENT}", f"{env_var}-"]
        op_detail = f"executando kubectl set env {env_var}-"
    else:
        argv = [kubectl, "-n", namespace, "set", "env",
                f"deploy/{_DISPATCH_DEPLOYMENT}", f"{env_var}={dispatcher}"]
        op_detail = f"executando kubectl set env {env_var}={dispatcher}"

    _audit_dispatch_stage_change(
        stage, dispatcher, result="allowed", detail=op_detail,
        namespace=namespace,
    )

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_dispatch_stage_change(
            stage, dispatcher, result="failed", detail=f"subprocess: {exc}",
            namespace=namespace,
        )
        return False, f"falha ao executar kubectl: {exc}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        _audit_dispatch_stage_change(
            stage, dispatcher, result="failed",
            detail=f"rc={proc.returncode} {err}", namespace=namespace,
        )
        return False, err

    msg = (proc.stdout or "rollout disparado").strip()
    _audit_dispatch_stage_change(
        stage, dispatcher, result="completed", detail=msg, namespace=namespace,
    )
    if dispatcher is None:
        return True, f"{env_var} unset ({msg})"
    return True, f"{env_var}={dispatcher} ({msg})"


# ===== Stage timeout / retries override — issue #391 =========================
#
# ``set_stage_timeout`` / ``set_stage_retries`` (e variantes clear) espelham
# exatamente o padrão de ``set_pipeline_dispatch_stage``:
#   * validam o stage contra PIPELINE_STAGES
#   * usam ``kubectl set env deploy/deile-pipeline`` para persistir
#   * emitem audit event ``SECURITY_POLICY_CHANGED``
#   * retornam ``(ok, msg)`` para o painel exibir ``last_msg``


def _audit_timeout_retries_change(
    stage: str, kind: str, value: Optional[int], *,
    result: str, detail: str, namespace: str,
) -> None:
    """Audit log wrapper for timeout/retries changes (espelha _audit_dispatch_stage_change)."""
    try:
        from deile.storage.audit_logger import AuditLogger, AuditEvent, AuditEventType, SeverityLevel  # noqa: PLC0415
        from datetime import datetime  # noqa: PLC0415
        AuditLogger.get_instance().log(AuditEvent(
            timestamp=datetime.now(),
            event_type=AuditEventType.SECURITY_POLICY_CHANGED,
            severity=SeverityLevel.INFO,
            operation=f"set_stage_{kind}",
            user="panel",
            details={
                "stage": stage, "kind": kind, "value": value,
                "result": result, "detail": detail, "namespace": namespace,
            },
        ))
    except Exception:  # noqa: BLE001
        pass


def set_stage_timeout(
    stage: str, timeout_s: Optional[int], *,
    namespace: str = NS, timeout: float = 15.0,
) -> tuple:
    """Pin ``DEILE_PIPELINE_TIMEOUT_S_<STAGE>=<timeout_s>`` em ``deile-pipeline``.

    Args:
        stage: canonical stage name (one of PIPELINE_STAGES).
        timeout_s: positive int (seconds) or None (clear the override).
        namespace: K8s namespace.
        timeout: subprocess timeout for kubectl.

    Returns ``(ok, msg)``.
    """
    try:
        from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES  # noqa: PLC0415
    except ImportError as exc:
        return False, f"dispatch_resolver indisponível: {exc}"

    if stage not in PIPELINE_STAGES:
        return False, f"invalid stage {stage!r} — esperado um de: {', '.join(PIPELINE_STAGES)}"

    if timeout_s is not None and timeout_s <= 0:
        return False, f"timeout_s deve ser > 0, got {timeout_s}"

    env_var = f"DEILE_PIPELINE_TIMEOUT_S_{stage.upper()}"
    kubectl = kubectl_bin()
    if kubectl is None:
        return False, "kubectl não encontrado"

    if timeout_s is None:
        argv = [kubectl, "-n", namespace, "set", "env",
                f"deploy/{_DISPATCH_DEPLOYMENT}", f"{env_var}-"]
    else:
        argv = [kubectl, "-n", namespace, "set", "env",
                f"deploy/{_DISPATCH_DEPLOYMENT}", f"{env_var}={timeout_s}"]

    _audit_timeout_retries_change(
        stage, "timeout_s", timeout_s, result="allowed",
        detail=f"kubectl set env {env_var}={timeout_s}", namespace=namespace,
    )
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"falha ao executar kubectl: {exc}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        return False, err

    msg = (proc.stdout or "rollout disparado").strip()
    if timeout_s is None:
        return True, f"{env_var} unset ({msg})"
    return True, f"{env_var}={timeout_s} ({msg})"


def set_stage_retries(
    stage: str, max_retries: Optional[int], *,
    allow_zero: bool = False,
    namespace: str = NS, timeout: float = 15.0,
) -> tuple:
    """Pin ``DEILE_PIPELINE_RETRIES_<STAGE>=<max_retries>`` em ``deile-pipeline``.

    Args:
        stage: canonical stage name (one of PIPELINE_STAGES).
        max_retries: non-negative int or None (clear the override).
            **Zero requer ``allow_zero=True`` explícito** — historicamente
            alguém confundiu zero com "default" e bloqueou o pipeline no
            primeiro fail. Zero é semanticamente "fail-fast, sem retry":
            pode ser desejável mas exige confirmação.
        allow_zero: quando ``True``, aceita ``max_retries=0`` como
            valor legítimo. Default ``False`` (rejeita zero com mensagem
            clara). Não afeta ``None`` (clear) nem inteiros positivos.
        namespace: K8s namespace.
        timeout: subprocess timeout for kubectl.

    Returns ``(ok, msg)``.
    """
    try:
        from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES  # noqa: PLC0415
    except ImportError as exc:
        return False, f"dispatch_resolver indisponível: {exc}"

    if stage not in PIPELINE_STAGES:
        return False, f"invalid stage {stage!r} — esperado um de: {', '.join(PIPELINE_STAGES)}"

    if max_retries is not None and max_retries < 0:
        return False, f"max_retries deve ser >= 0, got {max_retries}"

    if max_retries == 0 and not allow_zero:
        _audit_timeout_retries_change(
            stage, "max_retries", 0, result="denied",
            detail="zero rejeitado sem allow_zero — fail-fast precisa de confirmação",
            namespace=namespace,
        )
        return False, (
            "max_retries=0 = fail-fast (primeira falha bloqueia o stage). "
            "Para confirmar, digite '0!' no painel (force). "
            "Para usar o default, deixe vazio."
        )

    env_var = f"DEILE_PIPELINE_RETRIES_{stage.upper()}"
    kubectl = kubectl_bin()
    if kubectl is None:
        return False, "kubectl não encontrado"

    if max_retries is None:
        argv = [kubectl, "-n", namespace, "set", "env",
                f"deploy/{_DISPATCH_DEPLOYMENT}", f"{env_var}-"]
    else:
        argv = [kubectl, "-n", namespace, "set", "env",
                f"deploy/{_DISPATCH_DEPLOYMENT}", f"{env_var}={max_retries}"]

    _audit_timeout_retries_change(
        stage, "max_retries", max_retries, result="allowed",
        detail=f"kubectl set env {env_var}={max_retries}", namespace=namespace,
    )
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"falha ao executar kubectl: {exc}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        return False, err

    msg = (proc.stdout or "rollout disparado").strip()
    if max_retries is None:
        return True, f"{env_var} unset ({msg})"
    return True, f"{env_var}={max_retries} ({msg})"


# ===== Per-stage cost cap — issue #392 ======================================
#
# ``set_stage_cost_cap_usd`` / ``reset_stage_cost_cap_usd`` espelham
# :func:`set_pipeline_dispatch_stage`: escrevem / removem a env var
# ``DEILE_PIPELINE_COST_CAP_USD_<STAGE>`` no Deployment ``deile-pipeline``
# via ``kubectl set env``.  O resolver (:func:`resolve_stage_cost_cap_usd`
# em ``dispatch_resolver.py``) usa a env var como nível 1 da fallback chain.
#
# Deployment alvo: ``deile-pipeline`` (mesmo que as envs de dispatch e cost
# cap são lidas pelo pipeline, não pelo worker).

_COST_CAP_DEPLOYMENT = "deile-pipeline"

# Regex para valor decimal positivo de USD cost cap. Aceita "5", "5.00",
# ".50" — rejeita negativo, vazio, letras.
_COST_CAP_RE = re.compile(r"^\d*\.?\d+$")


def set_stage_cost_cap_usd(
    stage: str, usd: str, *, namespace: str = NS, timeout: float = 15.0,
) -> tuple:
    """Pin ``DEILE_PIPELINE_COST_CAP_USD_<STAGE>=<usd>`` em ``deile-pipeline``.

    Args:
        stage: canonical stage name (classify/refine/implement/pr_review/follow_ups).
        usd: positive decimal string, e.g. ``"5.00"``.
        namespace: K8s namespace.
        timeout: kubectl timeout.

    Returns:
        ``(ok: bool, msg: str)``.
    """
    try:
        from deile.orchestration.pipeline.dispatch_resolver import (  # noqa: PLC0415
            PIPELINE_STAGES)
    except ImportError as exc:
        return False, f"dispatch_resolver indisponível: {exc}"

    if stage not in PIPELINE_STAGES:
        allowed = ", ".join(PIPELINE_STAGES)
        return False, f"stage inválido {stage!r} — esperado um de: {allowed}"

    if not isinstance(usd, str) or not _COST_CAP_RE.match(usd.strip()):
        return False, (
            f"valor inválido {usd!r} — esperado decimal positivo (ex: '5.00')"
        )
    usd = usd.strip()

    kubectl = kubectl_bin()
    if kubectl is None:
        return False, "kubectl não encontrado"

    env_var = f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}"
    try:
        proc = subprocess.run(
            [kubectl, "-n", namespace, "set", "env",
             f"deploy/{_COST_CAP_DEPLOYMENT}", f"{env_var}={usd}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"falha ao executar kubectl: {exc}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        return False, err

    msg = (proc.stdout or "rollout disparado").strip()
    return True, f"{env_var}=${usd} ({msg})"


def reset_stage_cost_cap_usd(
    stage: str, *, namespace: str = NS, timeout: float = 15.0,
) -> tuple:
    """Remove ``DEILE_PIPELINE_COST_CAP_USD_<STAGE>`` de ``deile-pipeline``.

    Args:
        stage: canonical stage name.
        namespace: K8s namespace.
        timeout: kubectl timeout.

    Returns:
        ``(ok: bool, msg: str)``.
    """
    try:
        from deile.orchestration.pipeline.dispatch_resolver import (  # noqa: PLC0415
            PIPELINE_STAGES)
    except ImportError as exc:
        return False, f"dispatch_resolver indisponível: {exc}"

    if stage not in PIPELINE_STAGES:
        allowed = ", ".join(PIPELINE_STAGES)
        return False, f"stage inválido {stage!r} — esperado um de: {allowed}"

    kubectl = kubectl_bin()
    if kubectl is None:
        return False, "kubectl não encontrado"

    env_var = f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}"
    try:
        proc = subprocess.run(
            [kubectl, "-n", namespace, "set", "env",
             f"deploy/{_COST_CAP_DEPLOYMENT}", f"{env_var}-"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"falha ao executar kubectl: {exc}"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "kubectl set env falhou").strip()
        return False, err

    msg = (proc.stdout or "rollout disparado").strip()
    return True, f"{env_var} unset ({msg})"


# ===== Stage dispatch (worker + model) consolidado — issue #309 fase 2 ======
#
# ``StageDispatchProvider`` unifica 3 leituras separadas em uma única view:
#   1. Worker per-stage: ``DEILE_PIPELINE_DISPATCH_<STAGE>`` (env do
#      Deployment ``deile-pipeline``), com fallback no global
#      ``DEILE_PIPELINE_DISPATCH_MODE``; sem isso, default ``deile-worker``.
#   2. Model per-stage: ``DEILE_PIPELINE_MODEL_<STAGE>`` (env do Deployment
#      ``deile-worker``), com fallback no global ``DEILE_PIPELINE_MODEL``
#      (também conhecido como ``DEILE_PREFERRED_MODEL``).
#   3. Status do ``claude-worker``: Deployment aplicado? pod ready?
#      email logado (do Secret ``claude-credentials``)?
#
# Substitui (a partir da Task 21) ``DispatchModeProvider`` (PR #330) +
# ``StageModelsProvider`` (#305) — a view ``[d]`` consolidada do painel TUI
# vai consumir só este provider para evitar 3 fetches separados.
#
# Mantém TTL 3s alinhado com sibling providers (``CurrentModelProvider``,
# ``StageModelsProvider``, ``DispatchModeProvider``) — um único ``[r]``
# refresha tudo. Lê de DOIS Deployments (``deile-pipeline`` para worker
# dispatch, ``deile-worker`` para models) + UM Secret (``claude-credentials``
# para email), totalizando 3 ``kubectl get -o json`` por refresh.

_CLAUDE_WORKER_DEPLOYMENT = "claude-worker"
_CLAUDE_CREDENTIALS_SECRET = "claude-credentials"


@dataclass(frozen=True)
class StageDispatchEntry:
    """Snapshot per-stage do dispatcher + model + tuning resolvidos pelo runtime.

    - ``stage`` — canonical stage name (one of :data:`PIPELINE_STAGES`).
    - ``worker`` — qual worker pod receberá o dispatch deste stage:
      ``deile-worker`` (default) ou ``claude-worker``.
    - ``model`` — slug do modelo (ex.: ``anthropic:claude-opus-4-7``) ou
      ``None`` quando nenhum override per-stage nem global está setado.
    - ``source`` — ``"env"`` quando o WORKER veio de
      ``DEILE_PIPELINE_DISPATCH_<STAGE>`` (override específico do stage),
      ``"global"`` quando veio do fallback ``DEILE_PIPELINE_DISPATCH_MODE``,
      ``"default"`` quando ambos ausentes (cai no built-in ``deile-worker``).
    - ``timeout_s`` — override de timeout em segundos para este stage, ou
      ``None`` quando nenhum override per-stage está setado (cai no global).
    - ``max_retries`` — override de max retries para este stage, ou
      ``None`` quando nenhum override per-stage está setado (cai no global).
    - ``cost_cap_usd`` — per-run USD ceiling from
      ``DEILE_PIPELINE_COST_CAP_USD_<STAGE>`` env, or ``None`` if not set
      (issue #392).
    """

    stage: str
    worker: str
    model: Optional[str]
    source: str
    timeout_s: Optional[int] = None
    max_retries: Optional[int] = None
    cost_cap_usd: Optional[str] = None


@dataclass(frozen=True)
class ClaudeWorkerStatus:
    """Status operacional do Deployment ``claude-worker`` no cluster.

    - ``deployment_applied`` — True quando ``kubectl get deployment claude-worker``
      retorna manifest válido (i.e., o operador já aplicou o YAML).
    - ``pod_ready`` — True quando ``status.readyReplicas == status.replicas``
      e ``replicas > 0`` (pod live e healthy).
    - ``logged_in_email`` — email extraído de ``credentials.json`` no Secret
      ``claude-credentials`` (campo ``email`` do JSON base64-decoded). ``None``
      quando o Secret não existe, está malformado ou sem o campo email.
    """

    deployment_applied: bool
    pod_ready: bool
    logged_in_email: Optional[str]


class StageDispatchProvider(_KubectlProviderMixin):
    """Consolida leitura per-stage de worker + model + status claude-worker.

    A partir da Task 21 (issue #309 fase 2), substitui ``DispatchModeProvider``
    (PR #330) e ``StageModelsProvider`` (#305) na view unificada ``[d]`` do
    painel TUI — uma view única lê tudo de um provider só.

    O provider faz três fetches por refresh:
      * ``kubectl get deployment/deile-pipeline -o json`` — para a chain de
        worker dispatch (per-stage env + global env).
      * ``kubectl get deployment/deile-worker -o json`` — para a chain de
        model overrides per-stage + global.
      * ``kubectl get secret/claude-credentials -o json`` — para o email
        logado (best-effort; falha silenciosa não afeta as 5 entradas).

    TTL 3s espelha :class:`StageModelsProvider` e :class:`DispatchModeProvider`
    para um único ``[r]`` refrescar a view inteira sem disparada de subprocess
    desnecessária.
    """

    def __init__(self, ttl_s: float = 3.0, enabled: bool = True,
                 namespace: str = NS):
        self._kubectl = kubectl_bin()
        self._enabled = enabled
        self._namespace = namespace
        # Cache da lista de entries — fallback vazio quando provider desabilitado
        # ou cluster down. Errors são capturados pelo Cache.last_error.
        self._cache: Cache[List[StageDispatchEntry]] = Cache(
            ttl_s, self._fetch_entries, fallback=[],
        )
        # Cache separado do status claude-worker — TTL idêntico, fallback
        # neutro (deployment_applied=False) deixa a view mostrar "not applied".
        self._status_cache: Cache[ClaudeWorkerStatus] = Cache(
            ttl_s, self._fetch_status,
            fallback=ClaudeWorkerStatus(
                deployment_applied=False, pod_ready=False, logged_in_email=None,
            ),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get_all_stages(self, force: bool = False) -> List[StageDispatchEntry]:
        """Returns 5 entries (uma por stage de :data:`PIPELINE_STAGES`).

        ``force=True`` ignora TTL e re-fetcha do cluster. Usado pelo ``[r]``
        do painel; chamadas regulares (rendering loop) usam o TTL.
        """
        return self._cache.get(force)

    def get_claude_worker_status(self, force: bool = False) -> ClaudeWorkerStatus:
        """Status do Deployment ``claude-worker`` + email do Secret.

        Best-effort: falha de qualquer fetch cai no fallback neutro
        (``deployment_applied=False``, ``pod_ready=False``, ``email=None``).
        """
        return self._status_cache.get(force)

    # Fallback estático — espelha PIPELINE_STAGES + WORKER aliases canônicos do
    # dispatch_resolver. Usado quando o lazy import de ``deile.orchestration``
    # falha (ex.: syntax error em qualquer arquivo da cadeia de import por
    # merge conflict não resolvido). Provider não pode crashar o painel.
    _STAGES_FALLBACK = ("classify", "refine", "implement", "pr_review", "follow_ups")
    _DISPATCHER_ALIASES_FALLBACK = {
        "deile_worker": "deile-worker", "worker": "deile-worker",
        "deile": "deile-worker", "deile-worker": "deile-worker",
        "claude": "claude-worker", "claude_code": "claude-worker",
        "claude-code": "claude-worker", "claude-worker": "claude-worker",
    }

    def _fetch_entries(self) -> List[StageDispatchEntry]:
        """Lê env vars dos dois Deployments e monta as 5 entries.

        Lazy import de :data:`PIPELINE_STAGES` + :data:`_DISPATCHER_ALIASES`
        para não puxar ``deile.orchestration`` no boot do painel (cold-import
        custa ~200ms). Catch EXCEPTION (não só ImportError): qualquer erro
        na cadeia de import (incluindo SyntaxError vindo de merge conflict
        não resolvido em outro módulo) cai no fallback estático — provider
        NUNCA deve crashar o painel.
        """
        # Lazy import com fallback robusto — qualquer Exception (SyntaxError
        # vindo de merge conflict, ImportError de deps quebradas, etc.)
        # usa as constantes locais.
        try:
            from deile.orchestration.pipeline.dispatch_resolver import (  # noqa: PLC0415
                _DISPATCHER_ALIASES, PIPELINE_STAGES)
        except Exception:  # noqa: BLE001 — proteção genérica intencional
            PIPELINE_STAGES = self._STAGES_FALLBACK
            _DISPATCHER_ALIASES = self._DISPATCHER_ALIASES_FALLBACK

        # --local-only → cai no fallback vazio do Cache sem chamar kubectl.
        if not self._enabled:
            return [
                StageDispatchEntry(s, _DEFAULT_WORKER, None, "default",
                                   cost_cap_usd=None)
                for s in PIPELINE_STAGES
            ]

        def canonicalize(raw: str) -> str:
            """Worker alias → canônico (``deile-worker`` | ``claude-worker``)."""
            # Espelha :func:`_canonicalize` do dispatch_resolver, mas tolerante:
            # valor desconhecido devolve lowercase em vez de raise, pra o
            # painel mostrar o que está no cluster (visibility > strictness).
            return _DISPATCHER_ALIASES.get(raw.strip().lower(),
                                           raw.strip().lower())

        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        # Pipeline Deployment carrega o worker dispatch chain.
        pipeline_data = _capture_json(
            [self._kubectl, "-n", self._namespace, "get",
             f"deployment/{_DISPATCH_DEPLOYMENT}", "-o", "json"],
            timeout=4.0,
        )
        if not pipeline_data:
            # Pipeline absent → tudo default. Não levanta para deixar o
            # painel continuar a renderizar (cluster pode estar mid-deploy).
            return [
                StageDispatchEntry(s, _DEFAULT_WORKER, None, "default",
                                   cost_cap_usd=None)
                for s in PIPELINE_STAGES
            ]
        pipeline_env = StageModelsProvider._extract_env_map(pipeline_data)
        # Worker Deployment carrega o model chain (usa o mesmo extractor).
        worker_data = _capture_json(
            [self._kubectl, "-n", self._namespace, "get",
             f"deployment/{_STAGE_DEPLOYMENT}", "-o", "json"],
            timeout=4.0,
        )
        worker_env = (StageModelsProvider._extract_env_map(worker_data)
                      if worker_data else {})

        global_worker_raw = pipeline_env.get(_DISPATCH_ENV_VAR)
        # Aceita ``DEILE_PIPELINE_MODEL`` como alias canônico de
        # ``DEILE_PREFERRED_MODEL`` — ambos significam "model global default
        # do worker"; o painel usa o último como fonte autoritativa (paridade
        # com ``StageModelsProvider``).
        global_model = (worker_env.get("DEILE_PIPELINE_MODEL")
                        or worker_env.get("DEILE_PREFERRED_MODEL") or None)

        result: List[StageDispatchEntry] = []
        for stage in PIPELINE_STAGES:
            stage_worker_raw = pipeline_env.get(
                f"DEILE_PIPELINE_DISPATCH_{stage.upper()}"
            )
            stage_model_raw = worker_env.get(
                f"DEILE_PIPELINE_MODEL_{stage.upper()}"
            )
            # Worker chain: per-stage env → global env → default.
            # Canonicaliza via _DISPATCHER_ALIASES para apresentar nome no
            # formato canônico do dispatch_resolver (``deile-worker`` |
            # ``claude-worker``), que é o que a view ``[d]`` consolidada
            # espera (alinha com :func:`resolve_stage_dispatcher`).
            if stage_worker_raw and stage_worker_raw.strip():
                worker = canonicalize(stage_worker_raw)
                source = "env"
            elif global_worker_raw and global_worker_raw.strip():
                worker = canonicalize(global_worker_raw)
                source = "global"
            else:
                worker = _DEFAULT_WORKER
                source = "default"
            # Model chain: per-stage env → global env → None.
            model = ((stage_model_raw or "").strip() or global_model or None)
            # Timeout / retries overrides (issue #391) — read from pipeline
            # Deployment env vars. None = no override, cai no resolver chain.
            timeout_s: Optional[int] = None
            timeout_raw = pipeline_env.get(f"DEILE_PIPELINE_TIMEOUT_S_{stage.upper()}")
            if timeout_raw and timeout_raw.strip():
                try:
                    v = int(timeout_raw.strip())
                    if v > 0:
                        timeout_s = v
                except (ValueError, TypeError):
                    pass
            max_retries: Optional[int] = None
            retries_raw = pipeline_env.get(f"DEILE_PIPELINE_RETRIES_{stage.upper()}")
            if retries_raw is not None and retries_raw.strip():
                try:
                    v = int(retries_raw.strip())
                    if v >= 0:
                        max_retries = v
                except (ValueError, TypeError):
                    pass
            # Cost cap chain: per-stage env from pipeline Deployment (issue #392).
            cost_cap_raw = pipeline_env.get(
                f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}"
            )
            cost_cap_usd = (cost_cap_raw or "").strip() or None
            result.append(StageDispatchEntry(
                stage, worker, model, source,
                timeout_s=timeout_s, max_retries=max_retries,
                cost_cap_usd=cost_cap_usd,
            ))
        return result

    def _fetch_status(self) -> ClaudeWorkerStatus:
        """Lê Deployment ``claude-worker`` + Secret ``claude-credentials``.

        Fallback neutro quando provider desabilitado ou cluster down.
        Email é best-effort — falha do Secret não afeta o restante.
        """
        if not self._enabled:
            return ClaudeWorkerStatus(False, False, None)
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        deployment = _capture_json(
            [self._kubectl, "-n", self._namespace, "get",
             f"deployment/{_CLAUDE_WORKER_DEPLOYMENT}", "-o", "json"],
            timeout=4.0,
        )
        if not deployment:
            return ClaudeWorkerStatus(False, False, None)
        status = deployment.get("status", {}) or {}
        ready_replicas = status.get("readyReplicas", 0) or 0
        replicas = status.get("replicas", 0) or 0
        pod_ready = (ready_replicas == replicas) and replicas > 0
        email = self._read_claude_credentials_email()
        return ClaudeWorkerStatus(True, pod_ready, email)

    def _read_claude_credentials_email(self) -> Optional[str]:
        """Best-effort: lê ``credentials.json`` do Secret ``claude-credentials``.

        Retorna ``None`` em qualquer falha (Secret ausente, base64 malformado,
        JSON inválido, sem campo ``email``). Não levanta para não derrubar o
        ``_fetch_status`` inteiro por causa do Secret ausente.
        """
        if self._kubectl is None:
            return None
        secret = _capture_json(
            [self._kubectl, "-n", self._namespace, "get",
             f"secret/{_CLAUDE_CREDENTIALS_SECRET}", "-o", "json"],
            timeout=4.0,
        )
        if not secret:
            return None
        import base64  # noqa: PLC0415 (lazy — só quando o Secret existe)
        try:
            data_b64 = (secret.get("data", {}) or {}).get("credentials.json", "")
            if not data_b64:
                return None
            decoded = base64.b64decode(data_b64)
            payload = json.loads(decoded)
            email = payload.get("email")
            return email if isinstance(email, str) and email else None
        except (ValueError, json.JSONDecodeError, KeyError, TypeError):
            return None


# Default declarado em :data:`_DISPATCHER_ALIASES` (dispatch_resolver) —
# espelhado aqui para evitar import circular no fast path do _fetch_entries.
_DEFAULT_WORKER = "deile-worker"


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


def _try_load_status_client():
    """Importa :class:`StatusClient` se o pacote ``deile`` estiver disponível.

    O painel é desenhado pra rodar standalone (sem `pip install -e .`),
    então a integração com Fase 2 da issue #303 é best-effort: se o
    import falha, caímos no caminho legado (state file) sem reclamar.
    """
    try:
        from deile.runtime.status_server import StatusClient  # noqa: PLC0415
        return StatusClient
    except Exception:  # noqa: BLE001 — degradação silenciosa
        return None


_STATUS_CLIENT_CLS = _try_load_status_client()


class LocalInstancesProvider:
    """Lê `<runtime_dir>/*.json` (e opcionalmente o Unix socket Fase 2)
    e devolve snapshots por PID.

    Caminho preferencial (Fase 2 — issue #303): se o pacote `deile` está
    importável (`StatusClient` disponível) E existe um socket
    `<runtime_dir>/<instance_id>.sock`, o snapshot vem dele — mostra
    estado mais fresco que o último flush do state file (current_action
    pode estar segundos atrás no file mas é instantâneo no socket).
    Em qualquer falha (socket ausente, timeout, payload inválido) caímos
    no caminho legado: leitura do state file. Comportamento legado
    é preservado para painéis rodando sem `deile` instalado.

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
                 stale_after_s: float = _INSTANCE_STALE_AFTER_S,
                 socket_timeout_s: float = 0.25,
                 prefer_socket: bool = True):
        # Resolução lazy: env var > param > default global. Faz lookup no
        # construtor (não no import) para os testes poderem monkeypatchar
        # `DEILE_RUNTIME_DIR` antes de instanciar o provider.
        if runtime_dir is None:
            env = os.environ.get("DEILE_RUNTIME_DIR")
            runtime_dir = Path(env) if env else RUNTIME_DIR
        self._runtime_dir = runtime_dir
        self._stale_after_s = stale_after_s
        self._socket_timeout_s = socket_timeout_s
        # `prefer_socket=False` força o caminho legado — útil em testes
        # do próprio provider que querem isolar a leitura do file.
        self._prefer_socket = bool(prefer_socket) and _STATUS_CLIENT_CLS is not None
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
        # `registry.json` é da Fase 3 (índice de instances) — schema
        # diferente, lido pelo `LocalRegistryProvider`; pular aqui evita
        # "instance file invalid payload (no pid?)" no log.
        try:
            entries = [
                p for p in self._runtime_dir.glob("*.json")
                if p.name != "registry.json"
            ]
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
        self._gc_orphan_sockets(out)
        return out

    def _gc_orphan_sockets(self, alive: Dict[int, "InstanceSnapshot"]) -> None:
        """Remove sockets cujo state file sumiu (instance morta sem cleanup
        do socket — caso `kill -9` ou crash de event loop pré-atexit).

        Sem este GC, `~/.deile/run/` acumula `.sock` órfãos
        indefinidamente — operador vê leftovers no `ls`, e até pode
        confundir clients que tentem conectar num socket "morto".
        """
        try:
            sockets = list(self._runtime_dir.glob("*.sock"))
        except OSError:
            return
        live_stems = {f"{s.instance_id}" for s in alive.values()}
        for sock in sockets:
            if sock.stem in live_stems:
                continue
            self._unlink_quietly(sock)

    def _load_one(self, path: Path,
                  *, now: datetime) -> Optional[InstanceSnapshot]:
        # Caminho preferencial (Fase 2): se o socket está vivo, puxamos
        # diretamente — estado mais novo que o último flush. O socket
        # path é derivado do nome do file: `<id>.json` ↔ `<id>.sock`.
        payload = self._try_fetch_via_socket(path)
        if payload is None:
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
        # GC: PID morto → unlink silencioso do state file E do socket
        # par (mesma stem). Best-effort: se o unlink falhar (permissão,
        # FS readonly), apenas pulamos o snapshot — próximo tick tenta
        # de novo. Sockets órfãos sem state file correspondente são
        # cobertos por `_gc_orphan_sockets` no fim do `_fetch`.
        if not _pid_alive(snap.pid):
            self._unlink_quietly(path)
            self._unlink_quietly(path.with_suffix(".sock"))
            return None
        return snap

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        """Remove ``path`` ignorando FileNotFoundError; loga outros OSError."""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("GC unlink failed for %s: %s (will retry)", path, exc)

    def _try_fetch_via_socket(self, path: Path) -> Optional[Dict[str, Any]]:
        """Tenta puxar o snapshot do socket `<id>.sock` ao lado de `<id>.json`.

        Retorna None silenciosamente em qualquer falha — caller cai no
        caminho legado (state file). Pré-requisito: `StatusClient` no
        path (`deile` instalado) e socket existindo no diretório.
        """
        if not self._prefer_socket or _STATUS_CLIENT_CLS is None:
            return None
        sock_path = path.with_suffix(".sock")
        if not sock_path.exists():
            return None
        try:
            client = _STATUS_CLIENT_CLS(
                sock_path, timeout_s=self._socket_timeout_s,
            )
            return client.status()
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("socket fetch failed for %s: %s", sock_path, exc)
            return None


# ----- Local registry (Fase 3 — issue #303) ---------------------------------
#
# Provider opcional que lê `<runtime_dir>/registry.json` (compartilhado por
# todos os processos DEILE rodando no host) e devolve a lista de entries
# vivas. Útil pro painel mostrar uma linha "fleet" no header — "3 DEILE
# instances running" — sem precisar varrer o filesystem direto.
#
# Em paridade com `LocalInstancesProvider`: melhor tentar via `Registry` do
# pacote `deile` (que aplica file-lock e GC). Quando o pacote não está
# disponível, caímos numa leitura crua, sem GC e sem lock. O painel usa a
# lista só pra exibir contagem/lista — concorrência fica pra quem escreve.


def _try_load_registry_cls():
    """Importa :class:`Registry` se o pacote ``deile`` estiver disponível."""
    try:
        from deile.runtime.registry import Registry  # noqa: PLC0415
        from deile.runtime.registry import RegistryEntry
        return Registry, RegistryEntry
    except Exception:  # noqa: BLE001
        return None, None


_REGISTRY_CLS, _REGISTRY_ENTRY_CLS = _try_load_registry_cls()


@dataclass
class RegistrySnapshot:
    """Snapshot do registry — N entries vivas, mais um summary p/ header."""
    entries: List[Any] = field(default_factory=list)  # List[RegistryEntry]
    instances: int = 0

    @classmethod
    def empty(cls) -> "RegistrySnapshot":
        return cls(entries=[], instances=0)


class LocalRegistryProvider:
    """Lê `<runtime_dir>/registry.json` e expõe a lista de instâncias vivas.

    Quando o pacote `deile` está disponível, usa o :class:`Registry`
    completo (aplica file-lock + GC). Caso contrário, faz leitura crua
    do JSON (read-only, sem mutar o arquivo).

    Cache TTL default 3s — entries mudam pouco (só em start/stop de
    processo), valor maior reduz contenção no lock sem perceptível
    impacto na UI.
    """

    def __init__(self, runtime_dir: Optional[Path] = None,
                 ttl_s: float = 3.0):
        if runtime_dir is None:
            env = os.environ.get("DEILE_RUNTIME_DIR")
            runtime_dir = Path(env) if env else RUNTIME_DIR
        self._runtime_dir = runtime_dir
        self._registry_path = runtime_dir / "registry.json"
        self._cache: Cache[RegistrySnapshot] = Cache(
            ttl_s, self._fetch, fallback=RegistrySnapshot.empty(),
        )

    @property
    def runtime_dir(self) -> Path:
        return self._runtime_dir

    @property
    def registry_path(self) -> Path:
        return self._registry_path

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> RegistrySnapshot:
        return self._cache.get(force)

    def _fetch(self) -> RegistrySnapshot:
        if _REGISTRY_CLS is not None:
            try:
                reg = _REGISTRY_CLS(registry_path=self._registry_path)
                entries = reg.list(gc=True)
                return RegistrySnapshot(entries=list(entries),
                                        instances=len(entries))
            except Exception as exc:  # noqa: BLE001 — fallback p/ leitura crua
                logger.debug("Registry.list falhou: %s; usando leitura crua.", exc)
        # Fallback sem mutação — sem GC, sem lock; só pra exibir.
        if not self._registry_path.exists():
            return RegistrySnapshot.empty()
        try:
            payload = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return RegistrySnapshot.empty()
        if not isinstance(payload, dict):
            return RegistrySnapshot.empty()
        raw_entries = payload.get("instances", [])
        if not isinstance(raw_entries, list):
            return RegistrySnapshot.empty()
        # Não temos a classe RegistryEntry no fallback — devolvemos os
        # dicts diretamente. Quem consome trata via .get() ou indexação.
        return RegistrySnapshot(entries=list(raw_entries),
                                instances=len(raw_entries))


# ===== Pod metrics provider (kubectl top pod) ================================

class PodMetricsProvider(_KubectlProviderMixin):
    """Live CPU/memory usage per pod via `kubectl top pod`.

    TTL 5s — metrics-server scrape interval is 15s; no benefit from more
    frequent polling. Returns Dict[pod_name, (cpu_m, mem_b)]. Fails
    gracefully when metrics-server is absent (callers show dim '?').
    """

    def __init__(self, ttl_s: float = 5.0, namespace: str = NS,
                 enabled: bool = True):
        self._kubectl = kubectl_bin()
        self._namespace = namespace
        self._enabled = enabled
        self._cache: Cache[Dict[str, tuple]] = Cache(
            ttl_s, self._fetch, fallback={},
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> Dict[str, tuple]:
        return self._cache.get(force)

    def _fetch(self) -> Dict[str, tuple]:
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        out = _capture_text(
            [self._kubectl, "-n", self._namespace, "top", "pod", "--no-headers"],
            timeout=8.0,
        )
        if out is None:
            raise RuntimeError("kubectl top pod falhou (metrics-server ausente?)")
        return self._parse_top_output(out)

    @staticmethod
    def _parse_top_output(text: str) -> Dict[str, tuple]:
        result: Dict[str, tuple] = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            name, cpu_s, mem_s = parts[0], parts[1], parts[2]
            result[name] = (_parse_cpu(cpu_s), _parse_mem(mem_s))
        return result


# ===== Endpoint probe provider (kubectl get endpoints) =======================

@dataclass
class EndpointInfo:
    """Ready/not-ready pod names per Service in the namespace."""
    ready: Dict[str, set]       # service_name -> set of ready pod names
    not_ready: Dict[str, set]   # service_name -> set of not-ready pod names

    def service_for_pod(self, pod_name: str) -> Optional[str]:
        """Service name if pod appears in any endpoint subset, else None."""
        for svc, pods in self.ready.items():
            if pod_name in pods:
                return svc
        for svc, pods in self.not_ready.items():
            if pod_name in pods:
                return svc
        return None

    def is_ready(self, service: str, pod_name: str) -> bool:
        return pod_name in self.ready.get(service, set())

    @staticmethod
    def empty() -> "EndpointInfo":
        return EndpointInfo(ready={}, not_ready={})


class EndpointProbeProvider(_KubectlProviderMixin):
    """Single `kubectl get endpoints -o json` -> EndpointInfo (TTL 3s).

    Builds ready/not-ready pod-name sets per Service. Pods that don't
    appear in any endpoint (e.g. deile-pipeline) return None from
    service_for_pod — the view omits the ENDPOINT line for them.
    """

    def __init__(self, ttl_s: float = 3.0, namespace: str = NS,
                 enabled: bool = True):
        self._kubectl = kubectl_bin()
        self._namespace = namespace
        self._enabled = enabled
        self._cache: Cache[EndpointInfo] = Cache(
            ttl_s, self._fetch, fallback=EndpointInfo.empty(),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> EndpointInfo:
        return self._cache.get(force)

    def _fetch(self) -> EndpointInfo:
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")
        data = _capture_json(
            [self._kubectl, "-n", self._namespace, "get", "endpoints", "-o", "json"],
            timeout=5.0,
        )
        if data is None:
            raise RuntimeError("kubectl get endpoints falhou")
        return self._parse_endpoints(data)

    @staticmethod
    def _parse_endpoints(data: Any) -> EndpointInfo:
        ready: Dict[str, set] = {}
        not_ready: Dict[str, set] = {}
        for item in data.get("items", []):
            svc = item.get("metadata", {}).get("name", "")
            if not svc:
                continue
            for subset in (item.get("subsets") or []):
                for addr in (subset.get("addresses") or []):
                    ref = addr.get("targetRef", {})
                    if ref.get("kind") == "Pod":
                        pod = ref.get("name", "")
                        if pod:
                            ready.setdefault(svc, set()).add(pod)
                for addr in (subset.get("notReadyAddresses") or []):
                    ref = addr.get("targetRef", {})
                    if ref.get("kind") == "Pod":
                        pod = ref.get("name", "")
                        if pod:
                            not_ready.setdefault(svc, set()).add(pod)
        return EndpointInfo(ready=ready, not_ready=not_ready)


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
    stage_models: "StageModelsProvider"
    # Dispatch mode do pipeline (issue #309 — PR #330, global flip). Lê
    # ``DEILE_PIPELINE_DISPATCH_MODE`` da Deployment ``deile-pipeline`` para
    # mostrar/editar via panel TUI.
    dispatch_mode: "DispatchModeProvider"
    # Per-stage dispatch + model consolidados (issue #309 fase 2 — PR #336).
    # Lê DEILE_PIPELINE_DISPATCH_<STAGE> + DEILE_PIPELINE_MODEL_<STAGE> da
    # Deployment ``deile-pipeline`` + status do ``claude-worker`` Deployment
    # + email logado do Secret ``claude-credentials``. Consumido pela
    # ``DispatchMatrixView`` (hotkey [d]). TTL 3s.
    stage_dispatch: "StageDispatchProvider"
    notifier: NotifierProvider
    # PodWatch RESOURCES header (issue #394).
    pod_metrics: PodMetricsProvider
    endpoints: EndpointProbeProvider
    local_processes: Optional[LocalProcessesProvider] = None
    local_logs: Optional[LocalLogsProvider] = None
    local_audit: Optional[LocalAuditProvider] = None
    # Per-PID `current_action` (issue #303). Quando presente, o adapter
    # `_local_process_rows` prefere este provider ao fallback do log
    # global — assim cada processo DEILE mostra seu próprio "doing now"
    # em vez do mesmo texto compartilhado.
    local_instances: Optional["LocalInstancesProvider"] = None
    # Fleet view (issue #303 — Fase 3). Lê o registry compartilhado para
    # exibir contagem de DEILE instances no header. Opcional — quando
    # ausente, o painel só mostra a tabela LOCAL PROCESSES.
    local_registry: Optional["LocalRegistryProvider"] = None
    # WorkerProvider for the ``claude-worker`` deployment (issue #396).
    # ``None`` when the pod is not deployed (panel still renders without error).
    claude_workers: Optional[WorkerProvider] = None

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
        # `LocalRegistryProvider` segue a mesma regra — desabilitado em
        # `--k8s-only`. Fleet view só faz sentido quando o painel está
        # observando processos locais.
        local_registry = LocalRegistryProvider() if local_on else None
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
                                   enabled=k8s_on,
                                   costs=CostsProvider(db_path=context.usage_db)),
            # `context.repo` pode vir vazio se o operador construiu o
            # ctx direto (sem `.detect()`) — resolve no fallback global.
            # forge_kind do contexto (lido do deployment do NS) decide se
            # o provider usa ``gh api`` (GitHub) ou ``glab api`` (GitLab) —
            # evita o timeout de 15s/refresh do PR #297 quando um NS GitLab
            # tentava listar issues via ``gh`` num repo inexistente.
            github=GitHubProvider(
                repo=context.repo or REPO_DEFAULT,
                forge_kind=context.forge_kind,
            ),
            costs=CostsProvider(db_path=context.usage_db),
            models=ModelsProvider(),
            current_model=CurrentModelProvider(
                namespace=context.namespace,
                deployments=(context.worker_deploy, context.pipeline_deploy),
                enabled=k8s_on,
            ),
            # `StageModelsProvider` (issue #305) lê env vars per-stage da
            # mesma Deployment `deile-worker`. Hardcoded para `NS` global
            # internamente; só recebe `enabled` para respeitar `--local-only`.
            stage_models=StageModelsProvider(enabled=k8s_on),
            # `DispatchModeProvider` (issue #309) lê
            # ``DEILE_PIPELINE_DISPATCH_MODE`` da Deployment
            # ``deile-pipeline`` — single read, baixa frequência (3s).
            # ``namespace`` propagado do contexto (multi-NS / PR #315) — sem
            # isso o painel lê de ``deile`` mas escreve no NS escolhido,
            # quebrando a sincronia de leitura/escrita.
            dispatch_mode=DispatchModeProvider(enabled=k8s_on,
                                               namespace=context.namespace),
            # ``StageDispatchProvider`` (issue #309 fase 2 — PR #336)
            # é consumido pela ``DispatchMatrixView`` ([d]). Namespace
            # propagado para sincronia read/write (multi-NS PR #315) e
            # leitura do Deployment claude-worker no MESMO namespace que
            # o operador está vendo no painel.
            stage_dispatch=StageDispatchProvider(enabled=k8s_on,
                                                 namespace=context.namespace),
            notifier=NotifierProvider(namespace=context.namespace,
                                      deploy=context.bot_deploy,
                                      enabled=k8s_on),
            pod_metrics=PodMetricsProvider(namespace=context.namespace,
                                           enabled=k8s_on),
            endpoints=EndpointProbeProvider(namespace=context.namespace,
                                            enabled=k8s_on),
            local_processes=local_procs,
            local_logs=local_logs,
            local_audit=local_audit,
            local_instances=local_instances,
            local_registry=local_registry,
            claude_workers=WorkerProvider(
                namespace=context.namespace,
                worker_deploy="claude-worker",
                enabled=k8s_on,
                costs=CostsProvider(db_path=context.usage_db),
            ),
        )

    @classmethod
    def default(cls, repo: str = REPO_DEFAULT) -> "PanelData":
        """Backwards-compat: contexto padrão (namespace via env DEILE_K8S_NAMESPACE)."""
        return cls.from_context(RuntimeContext.detect(repo=repo))

    def _all_providers(self) -> tuple:
        """Ordem usada por `force_refresh_all` e `errors`."""
        base = (self.pods, self.pipeline, self.workers, self.github,
                self.costs, self.models, self.current_model,
                self.stage_models, self.dispatch_mode, self.stage_dispatch,
                self.notifier, self.pod_metrics, self.endpoints)
        optionals = tuple(p for p in (self.local_processes, self.local_logs,
                                      self.local_audit, self.local_instances,
                                      self.local_registry, self.claude_workers)
                          if p is not None)
        return base + optionals

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
                 "models", "current_model", "stage_models",
                 "dispatch_mode", "stage_dispatch", "notifier",
                 "pod_metrics", "endpoints"]
        if self.local_processes is not None:
            names.append("local_processes")
        if self.local_logs is not None:
            names.append("local_logs")
        if self.local_audit is not None:
            names.append("local_audit")
        if self.local_instances is not None:
            names.append("local_instances")
        if self.local_registry is not None:
            names.append("local_registry")
        out: List[tuple] = []
        for name, p in zip(names, self._all_providers()):
            err = p.last_error
            if not err:
                continue
            # "k8s desabilitado" e "kubectl não encontrado" são esperados —
            # não viram alerta. "kubectl top pod falhou" indica metrics-server
            # ausente — também esperado em clusters sem metrics-server.
            if ("k8s desabilitado" in err or "kubectl não encontrado" in err
                    or "kubectl top pod falhou" in err):
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

    # Frequência de checagem; fetch real respeita TTL. 1.0s é o
    # compromisso entre frescor visual (TTL dos providers locais é
    # 2s — não vale checar mais rápido que isso) e fork/exec
    # sustained (cada tick spawna até 8 subprocess.run; em sessões
    # longas, 0.5s gerava pressão de memória notável no macOS).
    DEFAULT_TICK_S = 1.0

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
