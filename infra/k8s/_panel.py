"""TUI ao vivo de monitoramento da stack DEILE no Kubernetes.

Painel full-screen com `rich.Live` que cruza o estado do cluster (pods,
deployments) com a fonte de verdade do pipeline (issues + PRs no
GitHub) e do uso (UsageRepository).

Composição em três módulos (todos em `infra/k8s/`):
- `_panel_data.py` — Cache TTL + providers (Pods, Pipeline, Worker,
  GitHub, Costs). Fonte única de verdade dos números.
- `_panel_demo.py` — mocks usados quando o cluster está fora ou o
  kubectl não está instalado (modo demo: UI ainda abre).
- `_panel.py` (este arquivo) — KeyReader (termios cbreak / msvcrt),
  view contract (`View`/`ActionResult`/`PanelApp`), alerts engine,
  adapters de rendering e o registry/loop principal.

Uso:
    python3 infra/k8s/deploy.py k8s panel     # entra no painel

Hotkeys globais (qualquer view):
    [1-5, a, m, n]  drill em sub-view (do dashboard)
    [esc]           volta à view anterior
    [q]             sai do painel
    [p]             pause / resume refresh automático
    [+] / [-]       acelera / desacelera o refresh (×0.25 a ×4)
    [r]             força refresh imediato (invalida caches dos providers)
    [s]             snapshot da tela em ~/.deile/snapshots/
    [?]             tela de ajuda
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import _panel_demo as demo  # noqa: E402
# Imports `_panel_data` e `_panel_demo` são unqualified — dependem do
# sys.path setup feito por `deploy.py` (que insere `infra/k8s/` no path
# antes de importar `_panel`). Não trocar para `from infra.k8s. ...` sem
# revisar como o orquestrador invoca o painel.
from _panel_data import \
    NS as _NS_DEFAULT  # noqa: F401  # PR #315 — multi-namespace
from _panel_data import _fmt_age  # noqa: F401
from _panel_data import _fmt_cpu_display, _fmt_mem_display, _pct  # noqa: F401
from _panel_data import EndpointInfo  # noqa: F401
from _panel_data import kubectl_bin  # noqa: F401
from _panel_data import BackgroundRefresher, PanelData  # noqa: F401
from _panel_data import \
    _audit_dispatch_mode_change as pd_audit_dispatch_mode_change
from _panel_data import \
    _audit_security_policy_change as pd_audit_security_policy_change
from _panel_data import \
    clear_pipeline_dispatch_mode as pd_clear_pipeline_dispatch_mode
from _panel_data import clear_stage_model as pd_clear_stage_model
from _panel_data import \
    set_pipeline_dispatch_mode as pd_set_pipeline_dispatch_mode
from _panel_data import \
    set_pipeline_dispatch_stage as pd_set_pipeline_dispatch_stage
from _panel_data import set_preferred_model as pd_set_preferred_model
from _panel_data import set_stage_model as pd_set_stage_model
from _panel_data import set_stage_timeout as pd_set_stage_timeout
from _panel_data import set_stage_retries as pd_set_stage_retries
from _panel_data import \
    set_stage_cost_cap_usd as pd_set_stage_cost_cap_usd
from _panel_data import \
    reset_stage_cost_cap_usd as pd_reset_stage_cost_cap_usd
from _panel_data import \
    set_pipeline_max_parallel as pd_set_pipeline_max_parallel
from _panel_data import \
    get_pipeline_max_parallel as pd_get_pipeline_max_parallel
from _panel_data import \
    get_claude_worker_replicas as pd_get_claude_worker_replicas
from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)

_HEALTH_LINE_RE = re.compile(r"GET /v1/health")

# Mapa estável (módulo-level) para encurtar nomes de workflow no header —
# montar a cada chamada em hot-path era desperdício.
_SHORT_LABELS = {
    "em_refinamento": "refine",
    "em_arquitetura": "arq",
    "em_implementacao": "impl",
    "em_pr": "pr",
    "aguardando_stakeholder": "aguard",
}

# Quantidade máxima de snapshots a manter em ~/.deile/snapshots/ — os
# mais antigos são apagados para o diretório não crescer indefinidamente.
_SNAPSHOT_RETAIN = 50

# Ordem de prioridade dos estados de workflow para sort_mode="status".
# Valores menores aparecem primeiro; estados ausentes ficam por último (99).
_SORT_WORKFLOW_ORDER: Dict[str, int] = {
    "em_implementacao": 0,
    "em_revisao": 1,
    "revisada": 2,
    "em_pr": 3,
    "nova": 4,
    "em_refinamento": 5,
    "em_arquitetura": 6,
    "decomposta": 7,
    "aguardando_stakeholder": 8,
    "bloqueada": 9,
}
_SORT_MODES = ("recent", "number", "status")


# ===== key reader ===========================================================

if os.name == "nt":  # pragma: no cover - Windows fallback
    import msvcrt

    class KeyReader:  # type: ignore[no-redef]
        """Variant Windows usando msvcrt.kbhit/getch."""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def read(self, timeout: float = 0.0) -> Optional[str]:
            end = time.monotonic() + max(timeout, 0)
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    try:
                        return ch.decode("utf-8")
                    except UnicodeDecodeError:
                        return None
                if time.monotonic() >= end:
                    return None
                time.sleep(0.01)
else:
    import termios
    import tty

    _ARROW = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}

    class KeyReader:
        """Lê uma tecla em cbreak mode, sem ecoar e sem bloquear.

        Usa ``os.read`` no FD bruto em vez de ``sys.stdin.read`` para
        bypassar o BufferedReader interno do TextIOWrapper — sem isso, o
        ``\\x1b`` da seta era puxado para o buffer e o ``select.select``
        seguinte (testando só o FD raw) retornava vazio, fazendo o reader
        confundir uma seta CSI com ESC sozinho. Resultado prático antes do
        fix: setas ↑/↓ não funcionavam.

        Decodifica ESC sozinho (vs prefix CSI) com timeout de 50ms — o
        suficiente para distinguir num terminal local sem deixar o usuário
        sentir lag.

        Restaura termios em qualquer caminho de saída (context-manager,
        atexit, SIGTERM/SIGHUP/SIGQUIT) — sem isso, um kill -TERM no painel
        deixa o shell do operador em cbreak mode.
        """

        # SIGINT é omitido propositalmente: o cbreak mode preserva `isig`
        # (Ctrl-C continua disparando SIGINT pelo terminal), e o KeyboardInterrupt
        # resultante já é capturado por `run_panel()` no entry point — não
        # precisamos do handler customizado pra restaurar termios nesse caminho.
        _SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)

        def __init__(self):
            self._fd: Optional[int] = (
                sys.stdin.fileno() if sys.stdin.isatty() else None
            )
            self._old = None
            self._restored = False
            self._prev_handlers: Dict[int, Any] = {}

        def __enter__(self):
            if self._fd is not None:
                self._old = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
                self._restored = False
                atexit.register(self._restore)
                for sig in self._SIGNALS:
                    try:
                        self._prev_handlers[sig] = signal.signal(
                            sig, self._signal_handler,
                        )
                    except (OSError, ValueError):
                        # Em threads não-main signal.signal levanta; ignorar.
                        pass
            return self

        def __exit__(self, *exc):
            self._restore()

        def _restore(self) -> None:
            if self._restored:
                return
            self._restored = True
            if self._fd is not None and self._old is not None:
                try:
                    termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
                except (OSError, termios.error):
                    pass
            for sig, prev in self._prev_handlers.items():
                try:
                    signal.signal(sig, prev)
                except (OSError, ValueError):
                    pass
            self._prev_handlers.clear()

        def _signal_handler(self, signum, frame):
            # Restaura o terminal e re-dispara o sinal com o handler default
            # — o processo sai com o status canônico do sinal.
            #
            # Trade-off: `termios.tcsetattr` e `signal.signal` não são
            # estritamente async-signal-safe pela POSIX, mas no CPython
            # típico (signal handlers executados entre instruções de
            # bytecode pelo eval loop, fora de syscalls bloqueantes) o
            # padrão é seguro o suficiente para um cleanup TUI best-effort.
            # A alternativa pure-safe (set flag + checar no loop principal)
            # exigiria um caminho de wakeup confiável a partir do
            # `select.select` em `KeyReader.read`, o que não é uma melhoria
            # proporcional ao risco aqui.
            self._restore()
            try:
                signal.signal(signum, signal.SIG_DFL)
            except (OSError, ValueError):
                pass
            os.kill(os.getpid(), signum)

        def read(self, timeout: float = 0.0) -> Optional[str]:
            if self._fd is None:
                return None
            if not select.select([self._fd], [], [], timeout)[0]:
                return None
            b = os.read(self._fd, 1)
            if not b:
                return None
            if b != b"\x1b":
                return b.decode("utf-8", errors="ignore") or None
            # ESC sozinho ou prefix CSI?
            if not select.select([self._fd], [], [], 0.05)[0]:
                return "ESC"
            seq = os.read(self._fd, 1)
            if seq != b"[":
                return "ESC"
            buf: List[bytes] = []
            while select.select([self._fd], [], [], 0.05)[0]:
                c = os.read(self._fd, 1)
                if not c:
                    break
                buf.append(c)
                if c.isalpha() or c == b"~":
                    break
            code = b"".join(buf).decode("utf-8", errors="ignore")
            return _ARROW.get(code, f"CSI:{code}")


# ===== view contract ========================================================

class Action(Enum):
    NOOP = "noop"
    BACK = "back"
    QUIT = "quit"
    NAV = "nav"
    REFRESH = "refresh"


@dataclass
class ActionResult:
    kind: Action = Action.NOOP
    target: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def quit(cls) -> "ActionResult":
        return cls(kind=Action.QUIT)

    @classmethod
    def back(cls) -> "ActionResult":
        return cls(kind=Action.BACK)

    @classmethod
    def nav(cls, target: str, **payload: Any) -> "ActionResult":
        return cls(kind=Action.NAV, target=target, payload=payload)

    @classmethod
    def refresh(cls) -> "ActionResult":
        return cls(kind=Action.REFRESH)


class View(ABC):
    """Sub-tela do painel: renderiza e responde a teclas.

    Sub-classes podem mudar `refresh_s` para uma cadência diferente
    (1s no pod-watch, 5s no timeline, 10s no GitHub etc).
    """

    name: str = "?"
    title: str = ""
    refresh_s: float = 3.0

    def on_mount(self, app: "PanelApp") -> None: ...
    def on_unmount(self, app: "PanelApp") -> None: ...

    @abstractmethod
    def render(self, app: "PanelApp") -> RenderableType: ...

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        return ActionResult()

    def intercepts_key(self, key: str) -> bool:
        """Allow the view to short-circuit a global hotkey.

        Returning True for *key* tells the dispatcher to deliver it to
        :meth:`handle_key` instead of running the global handler. Useful
        when a view has a modal state that must consume ESC (close modal)
        before the global ESC (pop view) fires.
        """
        return False


# ===== alerts engine ========================================================
#
# Regras simples, todas baseadas em snapshots dos providers. Cada regra
# retorna 0 ou 1 alerta — agregamos antes de renderizar. Mantemos no nível
# de módulo pra Fase 7 testar isoladamente sem montar a UI.

@dataclass
class Alert:
    severity: str  # 'warn' | 'crit'
    icon: str
    msg: str


def _alerts_from_data(data: Optional[PanelData]) -> List[Alert]:
    """Avalia thresholds contra o estado atual dos providers.

    Em modo demo (data is None), devolve o alerta sintético — para o
    operador ver como o painel se comporta com algo aceso.
    """
    if data is None:
        return [Alert("warn", icon, msg) for icon, msg in demo.ALERTS]

    out: List[Alert] = []
    # Pods restarting recentemente.
    for p in data.pods.get():
        if p.restarts >= 3:
            out.append(Alert(
                "crit", "⛔",
                f"{p.name} reiniciou {p.restarts} vez(es) — investigar",
            ))
        elif p.restarts >= 1 and p.age_s < 1800:
            out.append(Alert(
                "warn", "⚠",
                f"{p.name} reiniciou {p.restarts}× nos últimos 30min",
            ))
    # Pipeline ocioso por muito tempo (talvez tick travado).
    ps = data.pipeline.get()
    if ps.last_action_age_s is not None and ps.last_action_age_s > 300:
        out.append(Alert(
            "warn", "⚠",
            f"pipeline sem ação há {_fmt_age(ps.last_action_age_s)} — "
            "pode estar travado",
        ))
    # Issues bloqueadas em aberto.
    snap = data.github.get()
    blocked = [it for it in snap.issues if it.blocked]
    if blocked:
        nums = ", ".join(f"#{it.number}" for it in blocked[:3])
        more = f" (+{len(blocked) - 3})" if len(blocked) > 3 else ""
        out.append(Alert(
            "warn", "⚠",
            f"{len(blocked)} issue(s) com ~workflow:bloqueada: {nums}{more}",
        ))
    # Aguardando stakeholder (humano).
    awaiting = [it for it in snap.issues
                if it.workflow == "aguardando_stakeholder"]
    if awaiting:
        nums = ", ".join(f"#{it.number}" for it in awaiting[:3])
        out.append(Alert(
            "warn", "🙋",
            f"{len(awaiting)} issue(s) aguardando você: {nums}",
        ))
    # Provider errors.
    for name, err in data.errors():
        out.append(Alert("warn", "⚠", f"provider {name}: {err.split(':', 1)[0]}"))
    return out


# ===== activity feed ========================================================

@dataclass
class ActivityRow:
    hhmmss: str
    actor: str
    action: str
    target: str
    detail: str


def _activity_from_data(data: Optional[PanelData], limit: int = 8) -> List[ActivityRow]:
    if data is None:
        return [ActivityRow(*row) for row in demo.ACTIVITY[:limit]]
    # Combina eventos k8s + locais ordenando por timestamp desc — assim a
    # UI mostra atividade real seja qual for a fonte. Locais ganham
    # actor='local' (setado em LocalLogsState para diferenciar de pipeline).
    pool = list(data.pipeline.get().events)
    if data.local_logs is not None:
        pool.extend(data.local_logs.get().events)
    pool.sort(key=lambda ev: ev.ts, reverse=True)
    rows: List[ActivityRow] = []
    for ev in pool[:limit]:
        rows.append(ActivityRow(
            hhmmss=ev.hhmmss,
            actor=ev.actor,
            action=ev.action,
            target=ev.target,
            detail=ev.detail,
        ))
    return rows


def _last_activity_caption(data: Optional[PanelData]) -> Optional[str]:
    """Retorna string legível do evento mais recente, ex: '23s ago — #360 → em_pr'.

    Combina eventos do pipeline e locais (mesma fonte de `_activity_from_data`).
    Retorna None quando não há eventos para não poluir o rodapé.
    """
    if data is None:
        return None
    pool: List[Any] = list(data.pipeline.get().events)
    if data.local_logs is not None:
        pool.extend(data.local_logs.get().events)
    if not pool:
        return None
    ev = max(pool, key=lambda e: e.ts)
    age_s = (datetime.now(timezone.utc) - ev.ts).total_seconds()
    label = ev.detail[:40] if ev.detail else ev.action
    if ev.target:
        return f"{_fmt_age(age_s)} ago — {ev.target} → {label}"
    return f"{_fmt_age(age_s)} ago — {ev.actor} {label}"


# ===== pod adapter para a tabela ============================================

@dataclass
class PodRow:
    icon: str
    name: str
    role: str
    status: str
    age: str
    restarts: str
    last_activity: str
    doing_now: str
    busy: bool = False


def _local_process_rows(data: Optional[PanelData]) -> List[PodRow]:
    """Adapta `LocalProcessInfo` em `PodRow` (mesmo schema da tabela de pods).

    Permite o PodPickerView e o painel LOCAL PROCESSES reutilizarem o
    layout existente sem casos especiais — a UI vê só "rows" e usa o
    `role` (`local-*`) para colorir/dispatchar drill-in.

    Fonte do "doing now" por PID (issue #303): consulta primeiro o
    `LocalInstancesProvider` (state files publicados por cada processo).
    Se o PID tem snapshot, usa-o (atribuição correta por processo).
    Caso contrário, cai no log global do `LocalLogsProvider` —
    fallback de compat com processos legacy que ainda não publicam estado.
    Sem nenhuma das fontes, mostra cmdline + busy via CPU.
    """
    if data is None or data.local_processes is None:
        return []
    procs = data.local_processes.get()
    if not procs:
        return []
    instances = (data.local_instances.get()
                 if getattr(data, "local_instances", None) is not None
                 else {})
    log_state = (data.local_logs.get()
                 if data.local_logs is not None else None)
    now = datetime.now(timezone.utc)
    rows: List[PodRow] = []
    for p in procs:
        snap = instances.get(p.pid)
        if snap is not None:
            # Caminho preferencial: state file deste PID exato. Bug resolvido —
            # cada linha mostra o que SEU processo está fazendo.
            doing = snap.doing_now_label
            ref_ts = snap.current_action_started_at or snap.last_heartbeat_at
            if ref_ts is not None:
                last = _fmt_age((now - ref_ts).total_seconds()) + " ago"
            else:
                last = "—"
            busy = snap.current_action_kind in {"tool_execution", "llm_call"}
        elif log_state is not None and log_state.last_action_age_s is not None:
            # Fallback de compat: log global. Perde a atribuição por PID
            # (mesmo texto pra todos), mas é melhor que vazio.
            last = _fmt_age(log_state.last_action_age_s) + " ago"
            doing = log_state.last_action_summary[:48] or "idle"
            busy = log_state.last_action_age_s < 60
        else:
            last = "—"
            doing = p.cmd[:48] if p.cmd else "idle"
            busy = p.cpu_pct >= 1.0
        icon = "⚡" if busy else "●"
        # `status` reusa coluna; mostramos RSS pra dar densidade de info.
        rows.append(PodRow(
            icon=icon, name=p.name, role=p.role,
            status=p.rss_human, age=p.age_human,
            restarts=f"{p.cpu_pct:.0f}%",
            last_activity=last, doing_now=doing, busy=busy,
        ))
    return rows


def _pod_rows(data: Optional[PanelData], sort_mode: str = "recent") -> List[PodRow]:
    """Converte o estado dos providers em linhas da tabela de pods."""
    if data is None:
        return [
            PodRow(p.icon, p.name, p.role, p.status, p.age, p.restarts,
                   p.last_activity, p.doing_now, p.busy)
            for p in demo.PODS
        ]
    workers = data.workers.get()
    ps = data.pipeline.get()
    # Pares (age_s para sort, row) — age_s None significa "sem dado" (vai pro fim).
    pairs: List[Any] = []
    for p in data.pods.get():
        # Last-activity humano por role.
        if p.role == "worker":
            ws = workers.get(p.name)
            age_s: Optional[float] = ws.last_activity_s if ws else None
            last = _fmt_age(age_s) + " ago" if ws else "—"
            doing = ws.last_substantive_body[:32] if ws and ws.last_substantive_body \
                else ("ocupado" if ws and ws.busy else "idle")
            busy = bool(ws and ws.busy)
            icon = "⚡" if busy else "●"
        elif p.role == "pipeline":
            age_s = ps.last_action_age_s
            last = (_fmt_age(age_s) + " ago" if age_s is not None else "—")
            doing = ps.last_action_summary[:48] or "idle"
            busy = (age_s is not None and age_s < 60)
            icon = "⚡" if busy else "●"
        else:
            age_s = None
            last = "—"
            doing = "—"
            busy = False
            icon = "●"

        ready_label = p.status
        if p.status == "Running" and not p.ready:
            ready_label = "NotReady"

        row = PodRow(
            icon=icon, name=p.name, role=p.role,
            status=ready_label, age=_fmt_age(p.age_s),
            restarts=str(p.restarts), last_activity=last,
            doing_now=doing, busy=busy,
        )
        pairs.append((age_s, row))

    if sort_mode == "recent":
        # Menor age_s = mais recente; None vai pro fim.
        pairs.sort(key=lambda t: (t[0] is None, t[0] or 0.0))
    elif sort_mode == "number":
        pairs.sort(key=lambda t: t[1].name)
    elif sort_mode == "status":
        # Running primeiro; demais por nome como desempate.
        _pri = {"Running": 0, "NotReady": 1}
        pairs.sort(key=lambda t: (_pri.get(t[1].status, 2), t[1].name))

    return [row for _, row in pairs]


# ===== renderers reaproveitáveis ============================================
#
# Mantidos no nível de módulo para que as views da Fase 3+ reusem o mesmo
# estilo visual sem duplicar código.

def _head_panel(view_title: str, app: "PanelApp") -> Panel:
    """Cabeçalho dinâmico: modo (k8s/local/híbrido) + namespace + clock UTC + cadência."""
    paused = " ⏸ pausado" if app.paused else ""
    speed = "" if app.refresh_mult == 1.0 else f" ×{app.refresh_mult:g}"
    utc_now = datetime.now(timezone.utc)
    clock_utc = utc_now.strftime("%Y-%m-%d %H:%M:%S UTC")
    local_now = utc_now.astimezone()
    clock_local = local_now.strftime("%H:%M %z")
    head = Text()
    head.append("DEILE Stack", style="bold cyan")
    head.append("  ·  ")
    head.append(view_title, style="bold")
    head.append("  ·  ")
    head.append(clock_utc, style="dim")
    head.append(f"  ·  refresh {app.current_refresh_s:.1f}s{speed}{paused}",
                style="dim yellow" if app.paused else "dim")
    # Linha 2: contexto efetivo + conversão local do clock.
    if app.data is not None:
        ctx = app.data.context
        mode_style = ("bold green" if ctx.mode_label.startswith("k8s + local")
                      else "bold cyan" if "k8s" in ctx.mode_label
                      else "bold yellow" if "local" in ctx.mode_label
                      else "bold red")
        forge_txt = (ctx.forge_kind or "auto") if hasattr(ctx, "forge_kind") else "auto"
        sub = Text.assemble(
            ("mode: ", "dim"), (ctx.mode_label, mode_style),
            ("   cluster: ", "dim"), (ctx.cluster_label, "dim"),
            ("   namespace: ", "dim"), (ctx.namespace, "bold"),
            ("   forge: ", "dim"), (forge_txt, "bold magenta"),
            ("   repo: ", "dim"), (ctx.repo or "—", "dim"),
        )
    else:
        sub = Text("mode: demo (mocks)   cluster: —   namespace: —",
                   style="dim yellow")
    # Linha 3: conversão local do clock UTC (↳ hh:mm local).
    local_line = Text(f"↳ {clock_local} local", style="dim")
    pieces: List[RenderableType] = [head, sub, local_line]
    # Toasts efêmeros (snapshot salvo, etc) aparecem como linha extra
    # discreta no head — não quebram o layout das views.
    toasts = app.active_toasts()
    if toasts:
        toast_line = Text()
        for icon, msg in toasts[-2:]:
            toast_line.append(f"{icon} {msg}  ", style="bold yellow")
        pieces.append(toast_line)
    # Linha de memdebug quando ligado via --memdebug. Off por default.
    mem_line = app.memdebug_line()
    if mem_line:
        pieces.append(Text(mem_line, style="dim magenta"))
    return Panel(Group(*pieces), border_style="cyan", box=box.HEAVY)


def _footer_panel(hotkeys: str, last_activity: Optional[str] = None) -> Panel:
    """Rodapé com a linha de hotkeys e, opcionalmente, o indicador de última atividade."""
    if last_activity:
        content: Any = Group(
            Text(hotkeys, style="dim"),
            Text(f"Last activity: {last_activity}", style="dim"),
        )
    else:
        content = Text(hotkeys, style="dim")
    return Panel(content, border_style="dim", box=box.SIMPLE)


# ===== views ================================================================

class DashboardView(View):
    """Tela-mãe: pods + pipeline + activity + alerts + tokens.

    Quando `data` é None roda em modo demo (mocks); caso contrário lê
    dos providers do `_panel_data`. Cada `_*_panel` é resiliente — se um
    provider falhar, o painel correspondente desenha o último valor bom
    e o erro vai pro feed de alertas via `_alerts_from_data`.
    """

    name = "dashboard"
    title = "Dashboard"
    refresh_s = 1.0    # render é ~3ms; conteúdo refresca conforme TTL do provider

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.sort_mode: str = "recent"

    @property
    def HOTKEYS(self) -> str:
        return (
            "[1]Pod watch  [2]Pipeline  [3]Issues/PRs  [4]Logs split  "
            "[5]Tokens  [n]otifier  [a]ctions  [m]odel/runtime  "
            f"[d]ispatch (workers & models)  [s]ort:{self.sort_mode}  [?]help  [q]uit"
        )

    def render(self, app: "PanelApp") -> RenderableType:
        layout = Layout()
        local_rows = _local_process_rows(self.data)
        has_locals = bool(local_rows)
        k8s_on = (self.data is not None and self.data.context is not None
                  and self.data.context.k8s_available)
        # Três layouts possíveis no slot superior:
        # - híbrido (k8s + local): split lado a lado (PODS | LOCAL)
        # - só local: LOCAL ocupa o slot inteiro (PODS estaria vazio)
        # - só k8s (ou demo): PODS ocupa o slot inteiro (layout legado)
        children = [
            Layout(_head_panel(self.title, app), name="head", size=4),
        ]
        if has_locals and k8s_on:
            children.append(Layout(name="top_row", size=10))
        elif has_locals:
            children.append(Layout(
                self._local_processes_panel(local_rows),
                name="local", size=10,
            ))
        else:
            children.append(Layout(self._pods_panel(), name="pods", size=10))
        last_act = _last_activity_caption(self.data)
        children.extend([
            Layout(name="middle", size=8),
            Layout(self._activity_panel(), name="activity"),
            Layout(name="bottom", size=5),
            Layout(_footer_panel(self.HOTKEYS, last_act), name="footer",
                   size=4 if last_act else 3),
        ])
        layout.split_column(*children)
        if has_locals and k8s_on:
            layout["top_row"].split_row(
                Layout(self._pods_panel()),
                Layout(self._local_processes_panel(local_rows)),
            )
        layout["middle"].split_row(
            Layout(self._pipeline_panel()),
            Layout(self._alerts_panel()),
        )
        layout["bottom"].split_row(
            Layout(self._tokens_panel()),
            Layout(self._decisions_panel()),
        )
        return layout

    # --- panels ---

    def _pods_panel(self) -> Panel:
        rows = _pod_rows(self.data, sort_mode=self.sort_mode)
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        tbl.add_column(" ", width=2, no_wrap=True)
        tbl.add_column("pod", style="bold")
        tbl.add_column("status", width=8)
        tbl.add_column("age", width=5)
        tbl.add_column("r", width=2, justify="right")
        tbl.add_column("last-activity", width=12)
        tbl.add_column("doing now", no_wrap=False)
        for p in rows:
            icon_style = "bold yellow" if p.busy else "green"
            doing_style = "bold yellow" if p.busy else "dim"
            status_style = "green" if p.status == "Running" else "red"
            tbl.add_row(
                Text(p.icon, style=icon_style),
                p.name,
                Text(p.status, style=status_style),
                p.age,
                p.restarts,
                Text(p.last_activity, style="dim"),
                Text(p.doing_now, style=doing_style),
            )
        return Panel(tbl, title="[bold]PODS[/bold]",
                     title_align="left", border_style="cyan")

    def _local_processes_panel(self, rows: List[PodRow]) -> Panel:
        """Painel paralelo ao PODS para processos DEILE no host."""
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        tbl.add_column(" ", width=2, no_wrap=True)
        tbl.add_column("pid/role", style="bold")
        tbl.add_column("rss", width=6)
        tbl.add_column("up", width=5)
        tbl.add_column("cpu", width=5, justify="right")
        tbl.add_column("doing now", no_wrap=False)
        for p in rows:
            icon_style = "bold yellow" if p.busy else "green"
            doing_style = "bold yellow" if p.busy else "dim"
            tbl.add_row(
                Text(p.icon, style=icon_style),
                p.name,
                Text(p.status, style="dim"),
                p.age,
                p.restarts,
                Text(p.doing_now, style=doing_style),
            )
        return Panel(tbl, title="[bold]LOCAL PROCESSES[/bold] (host)",
                     title_align="left", border_style="magenta")

    def _pipeline_panel(self) -> Panel:
        if self.data is None:
            running = demo.PIPELINE["running_for_human"]
            last_age = demo.PIPELINE["last_action_age_human"]
            summary = demo.PIPELINE["last_action_summary"]
            dispatches = demo.PIPELINE["dispatches_24h"]
            mentions = demo.PIPELINE["mentions_24h"]
            issue_states = demo.ISSUE_STATES
            pr_states = demo.PR_STATES
            issues_n = sum(issue_states.values())
            prs_n = sum(pr_states.values())
        else:
            ps = self.data.pipeline.get()
            running = _fmt_age(ps.running_for_s)
            last_age = (_fmt_age(ps.last_action_age_s) + " ago"
                        if ps.last_action_age_s is not None else "—")
            summary = ps.last_action_summary
            dispatches = ps.dispatches_24h
            mentions = ps.mentions_24h
            # Fallback elegante quando o pipeline k8s não tem dados (ex:
            # rodando só localmente). Usa o LocalLogsState como proxy do
            # "pipeline" — quem está orquestrando.
            if (ps.last_action_ts is None
                    and self.data.local_logs is not None):
                ls = self.data.local_logs.get()
                if ls.last_action_ts is not None:
                    last_age = _fmt_age(ls.last_action_age_s) + " ago (local)"
                    summary = ls.last_action_summary
            snap = self.data.github.get()
            issue_states = snap.issue_states
            pr_states = snap.pr_states
            issues_n = len(snap.issues)
            prs_n = len(snap.prs)
        lines = [
            Text.assemble(
                ("running for ", "dim"),
                (running, "bold cyan"),
                ("   ·  last action ", "dim"),
                (last_age, "bold cyan"),
            ),
            Text.assemble(
                ("summary: ", "dim"),
                (summary[:60], "italic"),
            ),
            Text.assemble(
                (f"dispatches/24h: {dispatches}  ", "dim"),
                (f"mentions/24h: {mentions}", "dim"),
            ),
            Text.assemble(
                (f"Issues open: {issues_n}  ", "bold"),
                (_compact_state_counts(issue_states), "dim"),
            ),
            Text.assemble(
                (f"PRs open:    {prs_n}  ", "bold"),
                (_compact_state_counts(pr_states), "dim"),
            ),
        ]
        return Panel(Group(*lines), title="[bold]PIPELINE[/bold]",
                     title_align="left", border_style="magenta")

    def _activity_panel(self) -> Panel:
        rows = _activity_from_data(self.data, limit=10)
        if not rows:
            body: RenderableType = Text(
                "· sem atividade recente registrada", style="dim"
            )
        else:
            tbl = Table(box=box.SIMPLE, expand=True, show_header=False,
                        pad_edge=False)
            tbl.add_column(width=8, style="dim")
            tbl.add_column(width=10, style="bold cyan")
            tbl.add_column(width=12)
            tbl.add_column(width=8, style="yellow")
            tbl.add_column()
            for r in rows:
                tbl.add_row(r.hhmmss, r.actor, r.action, r.target,
                            Text(r.detail, style="dim"))
            body = tbl
        return Panel(body, title="[bold]ACTIVITY[/bold] (últimos 10)",
                     title_align="left", border_style="green")

    def _alerts_panel(self) -> Panel:
        alerts = _alerts_from_data(self.data)
        if not alerts:
            body: RenderableType = Text(
                "· sem alertas críticos", style="dim green",
            )
        else:
            lines = []
            for a in alerts[:6]:
                style = "bold red" if a.severity == "crit" else "bold yellow"
                lines.append(Text.assemble((f"{a.icon} ", style), a.msg))
            if len(alerts) > 6:
                lines.append(Text(f"… (+{len(alerts) - 6} mais)", style="dim"))
            body = Group(*lines)
        border = ("red" if any(a.severity == "crit" for a in alerts)
                  else "yellow" if alerts else "green")
        return Panel(body, title="[bold]ALERTS[/bold]",
                     title_align="left", border_style=border)

    def _tokens_panel(self) -> Panel:
        if self.data is None:
            providers = list(demo.TOKENS["providers"])
            total = float(demo.TOKENS["total_24h"])
            extra = f"records: {demo.TOKENS['records_24h']}  "  \
                    f"·  1h: ${demo.TOKENS['total_1h']:.2f}"
        else:
            c = self.data.costs.get()
            providers = sorted(c.by_provider_24h.items(),
                               key=lambda kv: -kv[1])
            total = c.total_24h
            extra = (f"records: {c.records_24h}  ·  "
                     f"1h: ${c.total_1h:.2f}")
        if providers:
            bits: List[Text] = []
            for prov, cost in providers:
                bits.append(Text.assemble(
                    (f"{prov} ", "dim"),
                    (f"${cost:.2f}", "bold green"),
                ))
            line = Text("   ").join(bits)
        else:
            line = Text("· sem registros de uso", style="dim")
        total_line = Text.assemble(
            ("total 24h: ", "dim"),
            (f"${total:.2f}", "bold green"),
            ("   ", ""),
            (extra, "dim"),
        )
        return Panel(Group(line, total_line),
                     title="[bold]TOKENS & CUSTOS[/bold]",
                     title_align="left", border_style="green")

    def _decisions_panel(self) -> Panel:
        if self.data is None:
            items = list(demo.DECISIONS)
        else:
            ps = self.data.pipeline.get()
            items = [(ev.target or "—", ev.detail)
                     for ev in reversed(ps.events[-5:])
                     if ev.action in {"mention", "stages", "dispatch"}][:3]
        if not items:
            body: RenderableType = Text("· sem decisões recentes", style="dim")
        else:
            lines = [
                Text.assemble((f"{ref}  ", "bold cyan"), (desc[:50], "dim"))
                for ref, desc in items
            ]
            body = Group(*lines)
        return Panel(body, title="[bold]ÚLTIMAS DECISÕES[/bold]",
                     title_align="left", border_style="blue")

    # --- key ---

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        nav = {
            "1": "pod-picker",
            "2": "pipeline-timeline",
            "3": "issues-prs",
            "4": "logs-split",
            "5": "tokens",
            "n": "notifier-echo",
            "a": "actions",
            "m": "model-switcher",
            # Pipeline Stage Configuration (issue #309 fase 2 — Task 21 cutover):
            # matriz unificada que substitui (a) o flip global de
            # ``DEILE_PIPELINE_DISPATCH_MODE`` da ``DispatchModeView`` (PR #330)
            # e (b) o per-stage model override da ``StageModelsView`` (#305).
            # A coluna ``Worker`` cobre o flip de despacho por stage e a coluna
            # ``Model`` cobre o override de modelo — ambos consolidados nesta
            # única view. As views legadas continuam no código (não foram
            # deletadas) mas saíram do registry — limpeza fica para FU PR.
            "d": "dispatch-mode-matrix",
        }
        if key in nav:
            return ActionResult.nav(nav[key])
        if key == "s":
            idx = _SORT_MODES.index(self.sort_mode)
            self.sort_mode = _SORT_MODES[(idx + 1) % len(_SORT_MODES)]
            return ActionResult.refresh()
        if key == "?":
            return ActionResult.nav("help")
        return ActionResult()


def _compact_state_counts(counts: Dict[str, int]) -> str:
    """Formata `{nova:2, em_impl:3}` em string compacta `nova:2  impl:3`."""
    if not counts:
        return "—"
    bits = []
    for k, v in counts.items():
        if v == 0:
            continue
        bits.append(f"{_SHORT_LABELS.get(k, k)}:{v}")
    return "  ".join(bits) if bits else "—"


class _LogStreamer:
    """Background `kubectl logs -f` que enche uma deque rolling.

    Pensado para ser dono curto-prazo: criado pelo `PodWatchView.on_mount`
    e parado no `on_unmount`. O processo `kubectl` recebe SIGTERM; após
    500ms sem morrer, SIGKILL (também não-bloqueante). A thread de
    leitura é daemon — segura contra vazamento se o app fechar antes do
    `stop`.
    """

    def __init__(self, kubectl: str, ns: str, pod: str,
                 tail: int = 50, maxlen: int = 300):
        self._cmd = [kubectl, "-n", ns, "logs", pod, "-f",
                     f"--tail={tail}", "--timestamps"]
        self.buf: Deque[str] = deque(maxlen=maxlen)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self._cmd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except OSError as exc:
            self.buf.append(f"[ERRO] não consegui rodar kubectl: {exc}")
            return
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            # Checa stop ANTES de processar — evita lag de 1 linha após
            # stop() e diminui o tempo até o break em buffers cheios.
            if self._stop.is_set():
                break
            self.buf.append(line.rstrip())

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=0.5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            except OSError:
                pass
            self._proc = None
        self._thread = None

    def snapshot(self, n: int = 30) -> List[str]:
        return list(self.buf)[-n:]


class PodPickerView(View):
    """Lista pods selecionável + ações de ciclo-de-vida.

    Hotkeys (além do enter pra abrir o PodWatch):

    - ``x`` — encerra o pod/processo selecionado (k8s: ``kubectl delete pod``;
      local: SIGTERM com escalation SIGKILL após 5s).
    - ``r`` — rollout restart do Deployment do pod (só k8s; em local mostra
      "não suportado").
    - ``R`` — rollout restart de TODOS os 4 deployments do stack k8s, em
      paralelo declarativo (loop best-effort: uma falha não aborta as
      demais).

    Toda ação destrutiva passa por confirmação inline ([y] confirma /
    qualquer outra cancela) e emite ``AuditEvent(COMMAND_EXECUTED)``.
    """

    name = "pod-picker"
    title = "Selecionar pod"
    refresh_s = 1.0

    HOTKEYS = (
        "[↑/↓] navega   [enter] entra   "
        "[x] kill   [r] restart   [R] restart-all-k8s   "
        "[esc] volta   [q] sai"
    )

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.cursor = 0
        # Confirmação inline: None = ocioso; "x"/"r"/"R" = aguardando [y]/[n].
        # Quando setado, o handler pula a navegação até resolver.
        self.confirm_action: Optional[str] = None
        # Texto do último resultado pra renderizar no panel de feedback;
        # ``last_ok`` controla a cor (verde ok / vermelho erro / amarelo info).
        self.last_msg: str = ""
        self.last_ok: Optional[bool] = None

    def _rows(self) -> List[PodRow]:
        """Lista pods do cluster + processos locais (na ordem natural)."""
        return _pod_rows(self.data) + _local_process_rows(self.data)

    @staticmethod
    def _deployment_for_role(role: str) -> Optional[str]:
        """Mapeia role do pod k8s pro nome do Deployment.

        Retorna None para roles locais (``local-*``) ou desconhecidas —
        sinal para o handler de ``r`` recusar a ação.
        """
        mapping = {
            "pipeline": "deile-pipeline",
            "worker":   "deile-worker",
            "bot":      "deilebot",
            "shell":    "deile-shell",
        }
        return mapping.get(role)

    @staticmethod
    def _pid_from_local_row(row: PodRow) -> Optional[int]:
        """Extrai PID de um row local — formato ``local-<role>#<pid>``.

        Retorna None se o formato não bater (defensivo; nunca deve
        acontecer na prática porque ``LocalProcessInfo.name`` sempre
        produz essa forma).
        """
        if not row.name or "#" not in row.name:
            return None
        try:
            return int(row.name.rsplit("#", 1)[1])
        except (ValueError, IndexError):
            return None

    def render(self, app: "PanelApp") -> RenderableType:
        rows = self._rows()
        if rows:
            self.cursor = max(0, min(self.cursor, len(rows) - 1))
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        tbl.add_column(" ", width=2)
        tbl.add_column(" ", width=2)
        tbl.add_column("pod", style="bold")
        tbl.add_column("role", width=10)
        tbl.add_column("status", width=10)
        tbl.add_column("age", width=6)
        tbl.add_column("doing now")
        for i, p in enumerate(rows):
            marker = "▶" if i == self.cursor else " "
            row_style = "bold cyan on grey15" if i == self.cursor else ""
            tbl.add_row(
                Text(marker, style="bold cyan"),
                Text(p.icon, style="bold yellow" if p.busy else "green"),
                Text(p.name, style=row_style),
                p.role,
                p.status,
                p.age,
                Text(p.doing_now, style="dim"),
            )

        # Painel de confirmação OU feedback da última ação. Mutuamente
        # exclusivos — a confirmação aparece enquanto está pendente, e
        # depois o feedback fica visível até a próxima ação (ou refresh).
        confirm_panel = self._confirm_panel(rows)

        body = Layout(name="body")
        if confirm_panel is not None:
            body.split_column(
                Layout(Panel(tbl, title="[bold]escolha um pod para assistir[/bold]",
                             title_align="left", border_style="cyan"),
                       name="list"),
                Layout(confirm_panel, name="confirm", size=7),
            )
        else:
            body.update(Panel(tbl,
                              title="[bold]escolha um pod para assistir[/bold]",
                              title_align="left", border_style="cyan"))

        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            body,
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout

    def _confirm_panel(self, rows: List[PodRow]) -> Optional[Panel]:
        """Renderiza o painel de confirmação OU o feedback da última ação."""
        if self.confirm_action is not None:
            text = self._describe_pending(rows)
            again = self.confirm_action
            return Panel(
                Text.from_markup(
                    text + "\n\n[bold yellow]Confirma?[/bold yellow] "
                    f"[bold green][y][/bold green] ou [bold green][{again}][/bold green] "
                    "novamente aplica  /  qualquer outra cancela",
                ),
                title="[bold]CONFIRMAR AÇÃO[/bold]",
                title_align="left", border_style="yellow",
            )
        if self.last_msg:
            border = ("green" if self.last_ok is True
                      else "red" if self.last_ok is False
                      else "yellow")
            style = ("bold green" if self.last_ok is True
                     else "bold red" if self.last_ok is False
                     else "bold yellow")
            return Panel(
                Text(self.last_msg, style=style),
                title="[bold]ÚLTIMA AÇÃO[/bold]",
                title_align="left", border_style=border,
            )
        return None

    def _describe_pending(self, rows: List[PodRow]) -> str:
        """Texto humano do que vai ser executado se o operador confirmar."""
        if self.confirm_action == "R":
            return (
                "[bold]rollout restart ALL[/bold] — todos os 4 deployments k8s "
                "(deile-pipeline, deile-worker, deilebot, deile-shell).\n"
                "Cada Deployment respeita sua strategy (RollingUpdate / Recreate)."
            )
        if not rows:
            return "(nenhum pod selecionado)"
        row = rows[min(self.cursor, len(rows) - 1)]
        if self.confirm_action == "x":
            if row.role.startswith("local-"):
                pid = self._pid_from_local_row(row)
                return (
                    f"[bold]kill local[/bold] pid={pid} "
                    f"([cyan]{row.name}[/cyan])\n"
                    "SIGTERM, com escalation pra SIGKILL após 5s se ignorado."
                )
            return (
                f"[bold]kubectl delete pod[/bold] [cyan]{row.name}[/cyan] "
                f"(role={row.role})\n"
                "O Deployment vai recriar o pod em segundos."
            )
        if self.confirm_action == "r":
            dep = self._deployment_for_role(row.role)
            return (
                f"[bold]kubectl rollout restart deployment/{dep}[/bold] "
                f"(pod selecionado: [cyan]{row.name}[/cyan])\n"
                "Reinicia TODOS os pods deste Deployment com a strategy do manifest."
            )
        return f"ação desconhecida: {self.confirm_action!r}"

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        # Resolução de confirmação SEMPRE primeiro — outras teclas ficam
        # mortas até o operador decidir. Padrão alinhado com ActionsView /
        # ModelSwitcherView.
        if self.confirm_action is not None:
            return self._handle_confirmation(key)

        rows = self._rows()
        n = len(rows)
        if key in ("UP", "k") and n:
            self.cursor = (self.cursor - 1) % n
            return ActionResult.refresh()
        if key in ("DOWN", "j") and n:
            self.cursor = (self.cursor + 1) % n
            return ActionResult.refresh()
        if key in ("\r", "\n") and n:
            pod = rows[self.cursor]
            return ActionResult.nav("pod-watch", pod_name=pod.name,
                                    pod_role=pod.role)

        # Ações destrutivas — abrem confirmação. ``R`` (maiúsculo) cobre
        # restart-all independente da row selecionada; ``x``/``r`` operam
        # na row sob o cursor (e exigem que haja alguma).
        if key == "R":
            self.last_msg = ""
            self.last_ok = None
            self.confirm_action = "R"
            return ActionResult.refresh()
        if key in ("x", "r"):
            if n == 0:
                self.last_msg = "nenhum pod selecionável"
                self.last_ok = False
                return ActionResult.refresh()
            row = rows[self.cursor]
            # ``r`` em row local não é suportado (não há "rollout" de PID;
            # use ``x`` pra matar e o operador re-executa manualmente).
            if key == "r" and row.role.startswith("local-"):
                self.last_msg = (
                    f"restart não suportado em processo local "
                    f"({row.name}) — use [x] pra matar"
                )
                self.last_ok = False
                return ActionResult.refresh()
            self.last_msg = ""
            self.last_ok = None
            self.confirm_action = key
            return ActionResult.refresh()
        return ActionResult()

    def _handle_confirmation(self, key: str) -> ActionResult:
        """Aplica ou cancela a ação pendente.

        Aceita confirmação por:
          1. ``y`` — universal (igual ao padrão de outras views).
          2. Repetir a própria tecla da ação (``x x``, ``r r``, ``R R``) —
             double-tap muscle-memory: o operador apertou ``x`` querendo
             matar, apertar ``x`` de novo confirma sem mover a mão.

        Qualquer outra tecla cancela (default-deny preservado).
        """
        action = self.confirm_action
        if key == "y" or key == action:
            try:
                self._apply(action)
            finally:
                self.confirm_action = None
            return ActionResult.refresh()
        from _panel_data import _audit_pod_action  # noqa: PLC0415
        _audit_pod_action(
            action or "?", resource="pending-confirmation",
            result="cancelled",
            detail=f"operador cancelou ({key!r})",
        )
        self.confirm_action = None
        self.last_msg = "cancelado pelo operador"
        self.last_ok = None
        return ActionResult.refresh()

    def _apply(self, action: Optional[str]) -> None:
        """Executa a ação destrutiva confirmada e popula last_msg/last_ok."""
        if self.data is None:
            self.last_msg = "modo demo — nenhuma ação aplicada"
            self.last_ok = False
            return
        from _panel_data import delete_pod  # noqa: PLC0415
        from _panel_data import (kill_local_pid, rollout_restart_all,
                                 rollout_restart_deployment)

        # Multi-NS (issue #297): propagar o namespace do contexto em vez de
        # deixar as funções caírem no default ``NS`` (env DEILE_K8S_NAMESPACE
        # ou "deile"). Sem isso, o operador no painel de ``deile-gl`` recebia
        # ``Error from server (NotFound): pods "<name>" not found`` porque o
        # kubectl rodava em ``-n deile``.
        ns = getattr(self.data.context, "namespace", None) or _NS_DEFAULT

        rows = self._rows()
        if action == "R":
            results = rollout_restart_all(namespace=ns)
            all_ok = all(ok for _, ok, _ in results)
            self.last_ok = all_ok
            self.last_msg = " | ".join(
                f"{dep}: {'OK' if ok else 'FAIL'} ({m[:40]})"
                for dep, ok, m in results
            )
            return
        if not rows:
            self.last_msg = "lista vazia — nada a aplicar"
            self.last_ok = False
            return
        row = rows[min(self.cursor, len(rows) - 1)]
        if action == "x":
            if row.role.startswith("local-"):
                pid = self._pid_from_local_row(row)
                if pid is None:
                    self.last_msg = f"PID inválido na linha {row.name!r}"
                    self.last_ok = False
                    return
                ok, msg = kill_local_pid(pid)
            else:
                ok, msg = delete_pod(row.name, namespace=ns)
            self.last_ok = ok
            self.last_msg = msg
            return
        if action == "r":
            dep = self._deployment_for_role(row.role)
            if dep is None:
                self.last_msg = (
                    f"role {row.role!r} não tem Deployment associado"
                )
                self.last_ok = False
                return
            ok, msg = rollout_restart_deployment(dep, namespace=ns)
            self.last_ok = ok
            self.last_msg = msg
            return
        self.last_msg = f"ação desconhecida: {action!r}"
        self.last_ok = False


def _open_path_in_editor(path: Path) -> tuple[bool, str]:
    """Abre `path` em editor de texto, sem bloquear o painel.

    Preferência: `cursor` -> `code` -> editor padrão da plataforma
    (Windows: `notepad`; macOS: `open -t`; Linux/outros: `xdg-open`,
    `gedit`, `nano`). Retorna `(sucesso, label_do_tool_usado)` para
    feedback transitório no rodapé.
    """
    # Editores preferidos primeiro — usuário pediu cursor > code.
    for bin_name in ("cursor", "code"):
        b = shutil.which(bin_name)
        if b is None:
            continue
        try:
            subprocess.Popen(
                [b, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, bin_name
        except OSError:
            continue
    if os.name == "nt":
        try:
            subprocess.Popen(["notepad", str(path)])
            return True, "notepad"
        except OSError:
            return False, ""
    if sys.platform == "darwin":
        opener = shutil.which("open")
        if opener is None:
            return False, ""
        try:
            # `open -t` força app de texto registrado (TextEdit por default).
            subprocess.Popen([opener, "-t", str(path)])
            return True, "open -t"
        except OSError:
            return False, ""
    for bin_name in ("xdg-open", "gedit", "nano"):
        b = shutil.which(bin_name)
        if b is None:
            continue
        try:
            subprocess.Popen(
                [b, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, bin_name
        except OSError:
            continue
    return False, ""


class _LocalLogTailer:
    """Equivalente ao `_LogStreamer` mas para `~/.deile/logs/deile.log`.

    Usa `tail -F` (segue rotação) quando disponível; caso ausente, faz
    polling-from-end manual num thread. Encerra com SIGTERM/SIGKILL como
    o `_LogStreamer`. Buffer rolling com mesma API (`snapshot`, `buf`).
    """

    def __init__(self, log_path: Path, tail_lines: int = 50,
                 maxlen: int = 400):
        tail = shutil.which("tail")
        self._cmd = ([tail, "-F", "-n", str(tail_lines), str(log_path)]
                     if tail else None)
        self._log_path = log_path
        self.buf: Deque[str] = deque(maxlen=maxlen)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._maxlen = maxlen

    def start(self) -> None:
        if self._proc is not None or self._thread is not None:
            return
        if self._cmd is not None:
            try:
                self._proc = subprocess.Popen(
                    self._cmd, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
            except OSError as exc:
                self.buf.append(f"[ERRO] tail falhou: {exc}")
                self._proc = None
            else:
                self._thread = threading.Thread(
                    target=self._reader, daemon=True,
                    name="local-log-tail",
                )
                self._thread.start()
                return
        # Fallback Python puro — polling do EOF a cada 0.5s.
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="local-log-poll",
        )
        self._thread.start()

    def _reader(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            self.buf.append(line.rstrip())

    def _poll_loop(self) -> None:
        last_size = 0
        if self._log_path.is_file():
            try:
                last_size = self._log_path.stat().st_size
            except OSError:
                last_size = 0
        while not self._stop.is_set():
            try:
                size = self._log_path.stat().st_size if self._log_path.is_file() else 0
            except OSError:
                size = 0
            if size > last_size:
                try:
                    with self._log_path.open("rb") as fh:
                        fh.seek(last_size)
                        chunk = fh.read(size - last_size).decode(
                            "utf-8", errors="replace",
                        )
                except OSError:
                    chunk = ""
                for ln in chunk.splitlines():
                    if self._stop.is_set():
                        break
                    self.buf.append(ln)
                last_size = size
            elif size < last_size:
                # Arquivo rotacionado — reseta o offset.
                last_size = 0
            self._stop.wait(timeout=0.5)

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=0.5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            except OSError:
                pass
            self._proc = None
        self._thread = None

    def snapshot(self, n: int = 30) -> List[str]:
        return list(self.buf)[-n:]


def _is_local_role(role: str) -> bool:
    """Helper compartilhado: identifica roles locais (`local-*`)."""
    return role.startswith("local-")


class PodWatchView(View):
    """Drill-in num pod (ou processo local): header + log live.

    K8s: `kubectl logs -f <pod> --tail=40 --timestamps` via `_LogStreamer`.
    Local: `tail -F ~/.deile/logs/deile.log` via `_LocalLogTailer`
    (fallback Python puro se `tail` ausente). Cabeçalho mostra estado
    do pod (k8s) ou metadados do processo (local-*).
    """

    name = "pod-watch"
    title = "Pod Watch"
    refresh_s = 1.0

    HOTKEYS = ("[f] follow on/off   [h] mostrar/esconder health   "
               "[c] clear log   [.] abrir log   [t] resize /tmp   "
               "[esc] volta   [q] sai")

    # Quantas linhas do buffer dumpamos no tempfile quando o usuário pede
    # "abrir log" num pod k8s. Suficiente pra contexto, sem inflar o disco.
    _DUMP_TAIL_LINES = 2000

    # Janela (s) em que a mensagem de status fica visível no header do log.
    _STATUS_TTL_S = 6.0

    # Presets de sizeLimit para /tmp (acionados pelas teclas 1-5 quando em
    # modo `[t]` resize). Cobrem desde shell ocioso (256Mi) até worker
    # rodando suíte completa do pytest com worktrees paralelas (5Gi).
    _TMP_PRESETS = (
        ("1", "256Mi"),
        ("2", "512Mi"),
        ("3", "1Gi"),
        ("4", "2Gi"),
        ("5", "5Gi"),
    )

    # Mapeia role do pod → Deployment alvo do kubectl patch. Roles locais
    # não têm Deployment (são processos no host) → resize não se aplica.
    _ROLE_TO_DEPLOYMENT = {
        "pipeline":     "deile-pipeline",
        "worker":       "deile-worker",
        "bot":          "deilebot",
        "shell":        "deile-shell",
        "claude-worker": "claude-worker",
    }

    # Roles com Service associado (issue #394): usados para decidir se a
    # linha ENDPOINT deve aparecer no header.  Roles ausentes (pipeline,
    # shell) não têm Service → linha omitida.
    _ROLE_TO_SERVICE = {
        "worker": "deile-worker",
        "bot":    "deilebot",
    }

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.pod_name: str = ""
        self.pod_role: str = ""
        self.streamer: Optional[Any] = None  # _LogStreamer ou _LocalLogTailer
        self.following: bool = True
        # Health-checks lotam o buffer em workers ociosos; por default escondemos.
        self.hide_health: bool = True
        # Feedback transitório (auto-limpa após _STATUS_TTL_S no render).
        self._status_msg: Optional[str] = None
        self._status_until: float = 0.0
        # Modo "aguardando preset de /tmp" — quando True, próxima tecla 1-5
        # aplica o preset correspondente; qualquer outra cancela.
        self._awaiting_tmp_preset: bool = False

    def on_mount(self, app: "PanelApp") -> None:
        # PodWatchView é singleton no registry — re-mount com pod diferente
        # precisa zerar TODO o estado, senão preferências (follow, hide_health)
        # vazam entre pods, confundindo o operador.
        self.following = True
        self.hide_health = True
        if self.streamer is not None:
            self.streamer.stop()
            self.streamer = None
        # Payload da navegação vem em `app.last_payload` (setado pelo PanelApp).
        payload = getattr(app, "last_payload", {}) or {}
        self.pod_name = payload.get("pod_name", "")
        self.pod_role = payload.get("pod_role", "")
        if not self.pod_name:
            return
        # Dispatch por role: locais → tail de log local; k8s → kubectl logs -f.
        if _is_local_role(self.pod_role):
            if self.data is not None and self.data.context is not None:
                log_path = self.data.context.logs_dir / "deile.log"
                self.streamer = _LocalLogTailer(log_path, tail_lines=40,
                                                maxlen=400)
                self.streamer.start()
            return
        kubectl = kubectl_bin()
        if kubectl is None:
            return
        ns = (self.data.context.namespace
              if self.data is not None and self.data.context is not None
              else _NS_DEFAULT)
        self.streamer = _LogStreamer(kubectl, ns, self.pod_name,
                                     tail=40, maxlen=400)
        self.streamer.start()

    def on_unmount(self, app: "PanelApp") -> None:
        if self.streamer is not None:
            self.streamer.stop()
            self.streamer = None

    def _header_body(self) -> RenderableType:
        if self.data is None:
            return Text("(modo demo — sem dados reais do pod)", style="dim yellow")
        # Locais: lê do LocalProcessesProvider, formato diferente do PodInfo.
        if _is_local_role(self.pod_role):
            return self._local_header_body()
        pod = next((p for p in self.data.pods.get() if p.name == self.pod_name), None)
        if pod is None:
            return Text(f"pod `{self.pod_name}` não encontrado.", style="red")
        if self.pod_role == "worker":
            wstate = self.data.workers.get().get(self.pod_name)
        elif self.pod_role == "claude-worker":
            wstate = (self.data.claude_workers.get().get(self.pod_name)
                      if self.data.claude_workers is not None else None)
        else:
            wstate = None
        # Fetch live metrics (graceful: empty dict when metrics-server absent).
        _raw_metrics = self.data.pod_metrics.get() if hasattr(self.data, "pod_metrics") else {}
        metrics_map = _raw_metrics if isinstance(_raw_metrics, dict) else {}
        live_cpu, live_mem = metrics_map.get(pod.name, (None, None))
        lines = [
            Text.assemble(
                ("name: ", "dim"), (pod.name, "bold"),
                ("   role: ", "dim"), (pod.role, "bold cyan"),
                ("   status: ", "dim"),
                (pod.status, "green" if pod.status == "Running" else "red"),
            ),
            Text.assemble(
                ("uptime: ", "dim"), (_fmt_age(pod.age_s), "bold"),
                ("   restarts: ", "dim"),
                (str(pod.restarts),
                 "bold red" if pod.restarts > 0 else "bold green"),
                ("   ready: ", "dim"),
                ("yes" if pod.ready else "NO",
                 "bold green" if pod.ready else "bold red"),
                ("   node: ", "dim"), (pod.node or "?", "dim"),
            ),
        ]

        # RESOURCES line (issue #394) — shown for all k8s pods.
        # Extract typed fields defensively: tests may use MagicMock for pod.
        _mem_lim = pod.mem_limit_bytes if isinstance(pod.mem_limit_bytes, int) else None
        _cpu_lim = pod.cpu_limit_millicores if isinstance(pod.cpu_limit_millicores, int) else None
        _oom_count = pod.oom_killed_count if isinstance(pod.oom_killed_count, int) else 0
        _last_oom = pod.last_oom_at if isinstance(pod.last_oom_at, datetime) else None

        mem_pct = _pct(live_mem, _mem_lim)
        cpu_pct = _pct(live_cpu, _cpu_lim)

        def _resource_style(pct: Optional[float]) -> str:
            if pct is None:
                return "bold green"
            if pct >= 85:
                return "bold red"
            if pct >= 60:
                return "bold yellow"
            return "bold green"

        mem_used_str = _fmt_mem_display(live_mem)
        mem_lim_str = _fmt_mem_display(_mem_lim)
        cpu_used_str = _fmt_cpu_display(live_cpu)
        cpu_lim_str = _fmt_cpu_display(_cpu_lim)
        mem_pct_str = f" ({mem_pct:.1f}%)" if mem_pct is not None else ""
        cpu_pct_str = f" ({cpu_pct:.1f}%)" if cpu_pct is not None else ""
        mem_style = _resource_style(mem_pct) if live_mem is not None else "dim"
        cpu_style = _resource_style(cpu_pct) if live_cpu is not None else "dim"

        oom_style = "bold red" if _oom_count > 0 else "dim green"
        if _oom_count > 0 and _last_oom is not None:
            now_utc = datetime.now(timezone.utc)
            oom_ago_s = (now_utc - _last_oom).total_seconds()
            oom_str = (f"last OOM {_fmt_age(oom_ago_s)} ago "
                       f"(×{_oom_count})")
        elif _oom_count > 0:
            oom_str = f"×{_oom_count} OOM"
        else:
            oom_str = "★ no OOM history"

        lines.append(Text.assemble(
            ("RESOURCES: ", "dim"),
            ("mem ", "dim"),
            (f"{mem_used_str}/{mem_lim_str}{mem_pct_str}", mem_style),
            ("  cpu ", "dim"),
            (f"{cpu_used_str}/{cpu_lim_str}{cpu_pct_str}", cpu_style),
            ("   ", ""),
            (oom_str, oom_style),
        ))

        # ENDPOINT line: only for pods with an associated Service.
        svc_name = self._ROLE_TO_SERVICE.get(pod.role)
        if svc_name is not None and hasattr(self.data, "endpoints"):
            ep_info = self.data.endpoints.get()
            if isinstance(ep_info, EndpointInfo) and not ep_info.is_ready(svc_name, pod.name):
                lines.append(Text.assemble(
                    ("ENDPOINT: ", "dim"),
                    (f"NOT in Service endpoints "
                     f"(kubectl get endpoints {svc_name})", "bold red"),
                ))

        if wstate is not None:
            lines.append(Text.assemble(
                ("worker: ", "dim"),
                ("BUSY" if wstate.busy else "idle",
                 "bold yellow" if wstate.busy else "dim"),
                ("   last activity: ", "dim"),
                (_fmt_age(wstate.last_activity_s) + " ago"
                 if wstate.last_activity_s is not None else "—", "bold"),
            ))
            # WORK line — replaces the old "current task:" block (issue #396).
            # Shows what the worker is doing right now, enriched with LLM model
            # and start time.  Forge-agnostic: target_label returns #N/PR#N/
            # mention … regardless of GitHub or GitLab (Decisão #42).
            ct = wstate.current_task
            if ct is not None:
                work_parts: List[Any] = [
                    ("WORK: ", "bold"),
                    (ct.target_label, "bold magenta"),
                ]
                if ct.branch:
                    work_parts.extend([
                        (" (", "dim"),
                        (ct.branch, "dim italic"),
                        (")", "dim"),
                    ])
                work_parts.extend([
                    (" | started ", "dim"),
                    (_fmt_age(ct.elapsed_s) + " ago", "bold"),
                ])
                if ct.model:
                    work_parts.extend([
                        (" | model ", "dim"),
                        (ct.model, "bold cyan"),
                    ])
                lines.append(Text.assemble(*work_parts))
            else:
                lines.append(Text.assemble(
                    ("WORK: ", "bold"),
                    ("— (idle)", "dim"),
                ))
            # LAST_COMPLETED line — omitted when no completed task in log buffer.
            lc = wstate.last_completed
            if lc is not None:
                outcome = lc.outcome.upper()
                if outcome in {"APPROVE", "OK", "DONE"}:
                    outcome_style = "bold green"
                elif outcome in {"REJECT", "FAIL", "ERROR"}:
                    outcome_style = "bold red"
                else:
                    outcome_style = "dim"
                # Build target label for the completed task via a temporary
                # CurrentTask (reuses the existing target_label logic).
                from _panel_data import CurrentTask as _CT  # noqa: PLC0415
                _ct_tmp = _CT(
                    task_id=lc.task_id, channel_id=lc.channel_id,
                    started_ts=lc.finished_ts,
                    stage=lc.stage, action_kind=lc.action_kind,
                    issue_number=lc.issue_number,
                )
                if lc.cost_usd is not None:
                    cost_str = f"${lc.cost_usd:.2f}"
                    cost_style = "bold"
                else:
                    cost_str = "$? (ledger unavailable)"
                    cost_style = "dim"
                finished_z = lc.finished_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                lines.append(Text.assemble(
                    ("LAST_COMPLETED: ", "bold"),
                    (_ct_tmp.target_label, "magenta"),
                    (" → ", "dim"),
                    (outcome, outcome_style),
                    (" | ", "dim"),
                    (_fmt_age(lc.duration_s), "bold"),
                    (" | ", "dim"),
                    (cost_str, cost_style),
                    (" | ", "dim"),
                    (finished_z, "dim"),
                ))
        # claude-worker specific sections (issue #395)
        if pod.role == "claude-worker":
            lines.extend(self._claude_worker_header_lines())
        return Group(*lines)

    def _claude_worker_header_lines(self) -> list:
        """LEASE / DISK / QUOTA lines for claude-worker pod header (issue #395)."""
        import time as _time
        lines = []
        if self.data is None or self.data.claude_worker_info is None:
            lines.append(Text.assemble(
                ("LEASE: ", "bold yellow"), ("— (provider unavailable)", "dim"),
            ))
            return lines

        status = self.data.claude_worker_info.get()

        # LEASE line
        lease = status.lease
        if lease:
            hb_age = _time.time() - float(lease.get("heartbeat_at") or 0)
            alive_label = "(alive)" if hb_age < 35 else "(stale)"
            alive_style = "green" if hb_age < 35 else "red"
            lines.append(Text.assemble(
                ("LEASE: ", "bold yellow"),
                ("task=", "dim"), (str(lease.get("task_id", "?")), "bold"),
                ("   heartbeat=", "dim"),
                (_fmt_age(hb_age) + " ago", "bold"),
                ("   ", ""),
                (alive_label, alive_style),
                ("   claudes_running=", "dim"),
                (str(status.claude_processes), "bold"),
            ))
        else:
            lines.append(Text.assemble(
                ("LEASE: ", "bold yellow"),
                ("— (idle)", "dim"),
                ("   claudes_running=", "dim"),
                (str(status.claude_processes), "bold"),
            ))

        # DISK line
        disk = status.disk
        if disk:
            used = int(disk.get("used_bytes") or 0)
            total = int(disk.get("total_bytes") or 1)
            pct = int(used * 100 / total) if total else 0
            used_mi = used // (1024 * 1024)
            total_gi = total / (1024 ** 3)
            disk_style = "red bold" if pct >= 80 else "bold"
            lines.append(Text.assemble(
                ("DISK: ", "bold cyan"),
                ("PVC claude-home ", "dim"),
                (f"{used_mi}Mi/{total_gi:.0f}Gi used ({pct}%)", disk_style),
            ))

        # QUOTA line — omitted when never captured
        quota = status.anthropic_quota
        if quota:
            tokens = int(quota.get("tokens_remaining") or 0)
            captured_at = float(quota.get("captured_at") or 0)
            age_s = _time.time() - captured_at
            lines.append(Text.assemble(
                ("QUOTA: ", "bold magenta"),
                ("anthropic tokens left ~", "dim"),
                (f" {tokens:,}", "bold"),
                (f"  (cached {_fmt_age(age_s)} ago, best-effort)", "dim"),
            ))

        return lines

    def _local_header_body(self) -> RenderableType:
        """Cabeçalho do drill-in para processo local (não k8s)."""
        if self.data is None or self.data.local_processes is None:
            return Text("(sem provider local — modo k8s-only?)", style="dim")
        procs = self.data.local_processes.get()
        proc = next((p for p in procs if p.name == self.pod_name), None)
        if proc is None:
            return Text(
                f"processo `{self.pod_name}` não encontrado "
                "(pode ter terminado).",
                style="red",
            )
        lines = [
            Text.assemble(
                ("pid: ", "dim"), (str(proc.pid), "bold"),
                ("   role: ", "dim"), (proc.role, "bold cyan"),
                ("   cpu: ", "dim"), (f"{proc.cpu_pct:.1f}%", "bold"),
                ("   rss: ", "dim"), (proc.rss_human, "bold"),
            ),
            Text.assemble(
                ("uptime: ", "dim"), (proc.age_human, "bold"),
                ("   source: ", "dim"),
                (str(self.data.context.logs_dir / "deile.log"), "dim"),
            ),
            Text.assemble(
                ("cmd: ", "dim"),
                (proc.cmd[:120], "dim italic"),
            ),
        ]
        return Group(*lines)

    def _log_panel(self) -> Panel:
        hidden = 0
        if self.streamer is None:
            body: RenderableType = Text("(streamer não iniciado)", style="dim")
        else:
            raw = self.streamer.snapshot(n=200)
            if self.hide_health:
                before = len(raw)
                raw = [ln for ln in raw if not _HEALTH_LINE_RE.search(ln)]
                hidden = before - len(raw)
            raw = raw[-30:]
            if not raw:
                body = Text(
                    "(sem linhas significativas — aguardando atividade real)"
                    if self.hide_health
                    else "(sem linhas no buffer ainda — aguardando log)",
                    style="dim",
                )
            else:
                body = Text("\n".join(raw), no_wrap=False)
        follow_label = "FOLLOW ON" if self.following else "PAUSED"
        health_label = ("health ESCONDIDOS" if self.hide_health
                        else "health VISÍVEIS")
        hidden_label = f"  ·  {hidden} health filtrados" if hidden else ""
        # Limpa status transitório expirado antes de compor o título.
        if self._status_msg is not None and time.time() >= self._status_until:
            self._status_msg = None
        status_label = (f"  ·  [yellow]{self._status_msg}[/yellow]"
                        if self._status_msg else "")
        title = (f"[bold]LIVE LOG[/bold]  ·  {follow_label}  ·  "
                 f"{health_label}{hidden_label}  ·  "
                 f"{self.pod_name}{status_label}")
        return Panel(body, title=title, title_align="left",
                     border_style="green" if self.following else "yellow")

    def render(self, app: "PanelApp") -> RenderableType:
        layout = Layout()
        # size=9 acomoda 6 linhas de header + bordas + título para k8s pods
        # (name/role/status + uptime + RESOURCES + ENDPOINT + worker/activity
        # + current task). claude-worker adiciona LEASE/DISK/QUOTA (até 3
        # linhas extra) → size=12. Outros pods mantêm 9.
        info_size = 12 if self.pod_role == "claude-worker" else 9
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(self._header_body(),
                         title="[bold]POD[/bold]", title_align="left",
                         border_style="cyan"),
                   name="info", size=info_size),
            Layout(self._log_panel(), name="log"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        # Resolve preset de /tmp PRIMEIRO — outras teclas ficam mortas até o
        # operador escolher ou cancelar (mesmo padrão do PodPickerView).
        if self._awaiting_tmp_preset:
            return self._handle_tmp_preset_key(key)
        if key == "f":
            self.following = not self.following
            if self.following and self.streamer is None:
                self.on_mount(app)
            elif not self.following and self.streamer is not None:
                self.streamer.stop()
                self.streamer = None
            return ActionResult.refresh()
        if key == "c" and self.streamer is not None:
            self.streamer.buf.clear()
            return ActionResult.refresh()
        if key == "h":
            self.hide_health = not self.hide_health
            return ActionResult.refresh()
        if key == ".":
            self._open_log_in_editor()
            return ActionResult.refresh()
        if key == "t":
            # Entra em modo "aguardando preset". Render mostra a hint no header.
            if _is_local_role(self.pod_role):
                self._set_status(
                    "resize /tmp não se aplica a processo local "
                    "(sem Deployment K8s)"
                )
                return ActionResult.refresh()
            if self._ROLE_TO_DEPLOYMENT.get(self.pod_role) is None:
                self._set_status(
                    f"role {self.pod_role!r} não tem Deployment associado"
                )
                return ActionResult.refresh()
            self._awaiting_tmp_preset = True
            self._set_status(
                "/tmp resize: [1]256Mi [2]512Mi [3]1Gi [4]2Gi [5]5Gi "
                "[esc/qualquer outra] cancela"
            )
            return ActionResult.refresh()
        return ActionResult()

    def _handle_tmp_preset_key(self, key: str) -> ActionResult:
        """Aplica o preset selecionado ou cancela.

        Tecla 1-5 → kubectl patch + rollout (sem confirmação extra: o usuário
        já optou pelo modo apertando [t]; cancela é default-deny).
        Qualquer outra tecla cancela silenciosamente.
        """
        self._awaiting_tmp_preset = False
        preset_map = dict(self._TMP_PRESETS)
        size = preset_map.get(key)
        if size is None:
            self._set_status("resize /tmp cancelado")
            return ActionResult.refresh()
        deployment = self._ROLE_TO_DEPLOYMENT.get(self.pod_role)
        if deployment is None:
            self._set_status(
                f"role {self.pod_role!r} não tem Deployment associado"
            )
            return ActionResult.refresh()
        # Multi-NS: usa o namespace do contexto (não o default global).
        ns = (self.data.context.namespace
              if self.data is not None and self.data.context is not None
              else _NS_DEFAULT)
        from _panel_data import set_pod_tmp_size  # noqa: PLC0415
        ok, msg = set_pod_tmp_size(deployment, size, namespace=ns)
        head = "OK" if ok else "FAIL"
        self._set_status(f"/tmp={size} {head}: {msg[:100]}")
        return ActionResult.refresh()

    def _set_status(self, msg: str) -> None:
        self._status_msg = msg
        self._status_until = time.time() + self._STATUS_TTL_S

    def _resolve_log_path_for_editor(self) -> Optional[Path]:
        """Devolve o caminho do arquivo a abrir.

        Processos locais usam o `deile.log` real. Pods k8s não têm
        arquivo local — dumpamos o buffer atual do streamer num tempfile
        de nome estável (por pod) e devolvemos ele.
        """
        if _is_local_role(self.pod_role):
            if self.data is None or self.data.context is None:
                return None
            return self.data.context.logs_dir / "deile.log"
        if self.streamer is None:
            return None
        try:
            lines = self.streamer.snapshot(n=self._DUMP_TAIL_LINES)
        except Exception:  # noqa: BLE001 — streamer pode estar em mau estado
            return None
        # Nome estável por pod: re-abrir sobrescreve, não polui /tmp.
        safe_pod = re.sub(r"[^A-Za-z0-9._-]", "_", self.pod_name or "pod")
        out_path = Path(tempfile.gettempdir()) / f"deile-podwatch-{safe_pod}.log"
        try:
            header = (
                f"# DEILE Pod Watch dump\n"
                f"# pod: {self.pod_name}\n"
                f"# role: {self.pod_role}\n"
                f"# captured_at: {datetime.now(timezone.utc).isoformat()}\n"
                f"# lines: {len(lines)} (buffer atual; rotaciona em "
                f"~/.deile/run e/ou kubectl logs)\n"
                f"# ----------------------------------------------------\n"
            )
            out_path.write_text(header + "\n".join(lines) + "\n",
                                encoding="utf-8")
        except OSError as exc:
            logger.warning("dump do buffer falhou: %s", exc)
            return None
        return out_path

    def _open_log_in_editor(self) -> None:
        path = self._resolve_log_path_for_editor()
        if path is None:
            self._set_status("não consegui resolver o arquivo de log")
            return
        if not path.exists():
            self._set_status(f"arquivo não existe: {path.name}")
            return
        ok, tool = _open_path_in_editor(path)
        if ok:
            self._set_status(f"abrindo em {tool}: {path.name}")
        else:
            self._set_status(
                "nenhum editor encontrado (cursor/code/notepad/open/xdg-open)"
            )


class PipelineTimelineView(View):
    """Timeline de eventos do pipeline + stats + histograma 24h."""

    name = "pipeline-timeline"
    title = "Pipeline Timeline"
    refresh_s = 1.0

    HOTKEYS = "[esc] volta   [r] força refresh   [q] sai"

    HIST_BUCKETS = 24      # 24 buckets de 1h cada
    _SPARK = " ▁▂▃▄▅▆▇█"

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data

    def _events(self):
        if self.data is None:
            return []
        return self.data.pipeline.get().events

    def _stats(self) -> Dict[str, Any]:
        evs = self._events()
        if not evs:
            return {"count": 0, "p95": None, "max": None, "avg": None,
                    "ticks_h": 0, "failures_h": 0,
                    "running_for": None, "last_age": None}
        # "Gap" entre eventos consecutivos como proxy de inatividade do pipeline.
        gaps: List[float] = []
        for i in range(1, len(evs)):
            delta = (evs[i].ts - evs[i - 1].ts).total_seconds()
            if delta >= 0:
                gaps.append(delta)
        ps = self.data.pipeline.get() if self.data else None
        return {
            "count": len(evs),
            "p95": _percentile(gaps, 0.95) if gaps else None,
            "max": max(gaps) if gaps else None,
            "avg": sum(gaps) / len(gaps) if gaps else None,
            "ticks_h": sum(1 for e in evs
                           if (datetime.now(timezone.utc) - e.ts).total_seconds() < 3600),
            "failures_h": sum(1 for e in evs
                              if e.action == "http"
                              and "→ 5" in (e.detail or "")),
            "running_for": ps.running_for_s if ps else None,
            "last_age": ps.last_action_age_s if ps else None,
        }

    def _histogram(self) -> str:
        """24 colunas (1 por hora) — densidade de events em cada hora."""
        now = datetime.now(timezone.utc)
        buckets = [0] * self.HIST_BUCKETS
        for e in self._events():
            age_h = (now - e.ts).total_seconds() / 3600
            if 0 <= age_h < self.HIST_BUCKETS:
                buckets[self.HIST_BUCKETS - 1 - int(age_h)] += 1
        if not any(buckets):
            return " " * self.HIST_BUCKETS
        peak = max(buckets)
        return "".join(self._SPARK[min(len(self._SPARK) - 1,
                                       int(v / peak * (len(self._SPARK) - 1)))]
                       for v in buckets)

    def render(self, app: "PanelApp") -> RenderableType:
        events = list(reversed(self._events()))  # mais recentes em cima
        stats = self._stats()
        # Tabela de eventos
        if events:
            tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
            tbl.add_column("time", width=8, style="dim")
            tbl.add_column("action", width=10, style="bold cyan")
            tbl.add_column("target", width=8, style="yellow")
            tbl.add_column("detail")
            for e in events[:30]:
                tbl.add_row(e.hhmmss, e.action, e.target,
                            Text(e.detail[:80], style="dim"))
            events_body: RenderableType = tbl
        else:
            events_body = Text(
                "(sem eventos — pipeline não logou nada nas últimas 200 linhas, "
                "ou está em modo demo)", style="dim")
        # Stats panel
        stats_lines = [
            Text.assemble(("events:    ", "dim"),
                          (str(stats["count"]), "bold")),
            Text.assemble(("ticks/1h:  ", "dim"),
                          (str(stats["ticks_h"]), "bold")),
            Text.assemble(("running:   ", "dim"),
                          (_fmt_age(stats["running_for"]) if stats["running_for"]
                           else "—", "bold")),
            Text.assemble(("last age:  ", "dim"),
                          (_fmt_age(stats["last_age"]) if stats["last_age"]
                           else "—", "bold")),
            Text.assemble(("gap p95:   ", "dim"),
                          (_fmt_age(stats["p95"]) if stats["p95"]
                           else "—", "bold")),
            Text.assemble(("gap max:   ", "dim"),
                          (_fmt_age(stats["max"]) if stats["max"]
                           else "—", "bold")),
            Text.assemble(("failures:  ", "dim"),
                          (str(stats["failures_h"]),
                           "bold red" if stats["failures_h"] else "bold green")),
        ]
        hist = self._histogram()
        hist_panel = Group(
            Text("events 24h (1 col = 1h, mais recente à direita):", style="dim"),
            Text(hist, style="bold cyan"),
            Text(("├" + "─" * (self.HIST_BUCKETS - 2) + "┤"), style="dim"),
            Text(f"{'-24h':<{self.HIST_BUCKETS - 4}}now", style="dim"),
        )
        last_act = _last_activity_caption(self.data)
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(name="middle", size=10),
            Layout(Panel(events_body,
                         title="[bold]EVENTS (mais recentes em cima)[/bold]",
                         title_align="left", border_style="green"),
                   name="events"),
            Layout(_footer_panel(self.HOTKEYS, last_act), name="footer",
                   size=4 if last_act else 3),
        )
        layout["middle"].split_row(
            Layout(Panel(Group(*stats_lines),
                         title="[bold]STATS[/bold]", title_align="left",
                         border_style="magenta")),
            Layout(Panel(hist_panel,
                         title="[bold]HISTOGRAMA 24h[/bold]",
                         title_align="left", border_style="blue")),
        )
        return layout


def _percentile(values: List[float], p: float) -> Optional[float]:
    """Percentil simples (interpolação linear); None se vazio."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


class IssuesPRsView(View):
    """Monitor de work-items forge-agnóstico (issues, PRs GitHub e MRs GitLab)."""

    name = "issues-prs"
    title = "Work-Items (WI)"
    refresh_s = 1.0

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.filter: str = "all"
        self.cursor: int = 0
        self.sort_mode: str = "recent"
        # Sem nome hardcoded: tenta env GH_USER → `gh api user` → vazio.
        self.my_login: str = self._resolve_login()

    @property
    def HOTKEYS(self) -> str:
        return (
            f"[a] all   [i] só issues   [p] só PRs   [b] só bloqueadas   "
            f"[m] minhas   [s]ort:{self.sort_mode}   [↑/↓] navega   "
            f"[enter] abrir URL   [esc] volta"
        )

    @staticmethod
    def _resolve_login() -> str:
        env = os.environ.get("GH_USER")
        if env:
            return env
        gh = shutil.which("gh")
        if gh is None:
            return ""
        try:
            out = subprocess.run(
                [gh, "api", "user", "-q", ".login"],
                capture_output=True, text=True, timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if out.returncode == 0:
            return out.stdout.strip()
        return ""

    def _rows(self):
        if self.data is None:
            return [], []
        snap = self.data.github.get()
        issues: List[Any] = list(snap.issues)
        prs: List[Any] = list(snap.prs)
        if self.filter == "i":
            prs = []
        elif self.filter == "p":
            issues = []
        elif self.filter == "b":
            issues = [it for it in issues if it.blocked]
            prs = [pr for pr in prs if pr.blocked]
        elif self.filter == "m":
            issues = [it for it in issues if self.my_login in it.assignees]
            prs = [pr for pr in prs if self.my_login in pr.assignees]
        # Aplica sort_mode (estado local da view — não global).
        if self.sort_mode == "recent":
            # updated_at desc; None vai pro fim.
            _key_r = lambda it: (it.updated_at is None,  # noqa: E731
                                 -(it.updated_at.timestamp() if it.updated_at else 0))
            issues.sort(key=_key_r)
            prs.sort(key=_key_r)
        elif self.sort_mode == "number":
            issues.sort(key=lambda it: it.number)
            prs.sort(key=lambda it: it.number)
        elif self.sort_mode == "status":
            _key_s = lambda it: _SORT_WORKFLOW_ORDER.get(it.workflow or "", 99)  # noqa: E731
            issues.sort(key=_key_s)
            prs.sort(key=_key_s)
        return issues, prs

    def _flat(self):
        issues, prs = self._rows()
        return list(issues) + list(prs)

    @staticmethod
    def _ci_chip(ci_status: str) -> Text:
        _CI_STYLES = {
            "passing": ("✓CI", "bold green"),
            "failing": ("✗CI", "bold red"),
            "pending": ("~CI", "bold yellow"),
            "none": ("—", "dim"),
        }
        label, style = _CI_STYLES.get(ci_status, ("—", "dim"))
        return Text(label, style=style)

    @staticmethod
    def _merge_chip(mergeability: str) -> Text:
        _MERGE_STYLES = {
            "clean":    ("✓MG", "bold green"),
            "conflict": ("✗MG", "bold red"),
            "draft":    ("DRFT", "dim yellow"),
            "blocked":  ("BLKD", "bold red"),
            "unknown":  ("?", "dim"),
        }
        label, style = _MERGE_STYLES.get(mergeability, ("?", "dim"))
        return Text(label, style=style)

    def _build_table(self, items, label: str, *, show_wi_cols: bool = False) -> Panel:
        if not items:
            return Panel(Text(f"· nada em {label}", style="dim"),
                         title=f"[bold]{label.upper()}[/bold]",
                         title_align="left", border_style="dim")
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        # Larguras como `max_width` (teto, não literal — princípio 15): Rich
        # pode encolher cada coluna abaixo desse valor quando o terminal é
        # estreito, em vez de travar a tabela. A coluna `title` fica sem teto
        # para absorver o espaço restante.
        tbl.add_column(" ", max_width=2)
        tbl.add_column("#", max_width=6)
        tbl.add_column("workflow", max_width=22)
        tbl.add_column("review", max_width=14)
        if show_wi_cols:
            tbl.add_column("ci", max_width=5)
            tbl.add_column("merge", max_width=5)
            tbl.add_column("reviewers", max_width=20)
        tbl.add_column("updated", max_width=10)
        tbl.add_column("assignees", max_width=18)
        tbl.add_column("title")
        now = datetime.now(timezone.utc)
        flat = self._flat()
        for it in items:
            global_idx = flat.index(it)
            marker = "▶" if global_idx == self.cursor else " "
            wf_style = "bold red" if it.blocked else "cyan"
            age_s = ((now - it.updated_at).total_seconds()
                     if it.updated_at else None)
            row: list = [
                Text(marker, style="bold cyan"),
                str(it.number),
                Text(it.workflow or "—", style=wf_style),
                Text(it.review or "—", style="magenta" if it.review else "dim"),
            ]
            if show_wi_cols:
                rvs = ", ".join(r.login for r in it.requested_reviewers) or "—"
                row += [
                    self._ci_chip(it.ci_status),
                    self._merge_chip(it.mergeability),
                    Text(rvs[:20], style="cyan" if it.requested_reviewers else "dim"),
                ]
            row += [
                _fmt_age(age_s),
                ", ".join(it.assignees) or "—",
                Text(it.title[:60], style="dim"),
            ]
            tbl.add_row(*row)
        return Panel(tbl, title=f"[bold]{label.upper()}[/bold]",
                     title_align="left", border_style="cyan")

    def render(self, app: "PanelApp") -> RenderableType:
        issues, prs = self._rows()
        flat = list(issues) + list(prs)
        if flat:
            self.cursor = max(0, min(self.cursor, len(flat) - 1))
        filter_label = {
            "all": "todos", "i": "só issues", "p": "só PRs/MRs",
            "b": "só bloqueadas", "m": f"meus (@{self.my_login})",
        }[self.filter]
        filter_panel = Panel(
            Text.assemble(
                ("filtro: ", "dim"), (filter_label, "bold yellow"),
                ("    issues: ", "dim"), (str(len(issues)), "bold"),
                ("    PRs/MRs: ", "dim"), (str(len(prs)), "bold"),
            ),
            border_style="dim",
        )
        last_act = _last_activity_caption(self.data)
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(filter_panel, name="filter", size=3),
            Layout(self._build_table(issues, "Issues"), name="issues"),
            Layout(self._build_table(prs, "PRs / MRs", show_wi_cols=True),
                   name="prs"),
            Layout(_footer_panel(self.HOTKEYS, last_act), name="footer",
                   size=4 if last_act else 3),
        )
        return layout

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        flat = self._flat()
        if key in ("a", "i", "p", "b", "m"):
            self.filter = key
            self.cursor = 0
            return ActionResult.refresh()
        if key == "s":
            idx = _SORT_MODES.index(self.sort_mode)
            self.sort_mode = _SORT_MODES[(idx + 1) % len(_SORT_MODES)]
            self.cursor = 0
            return ActionResult.refresh()
        n = len(flat)
        if n == 0:
            return ActionResult()
        if key in ("UP", "k"):
            self.cursor = (self.cursor - 1) % n
            return ActionResult.refresh()
        if key in ("DOWN", "j"):
            self.cursor = (self.cursor + 1) % n
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            url = flat[self.cursor].url
            if url:
                # Abre no browser default do OS (forge-agnóstico — a URL já
                # vem do `html_url` do GitHub ou do `web_url` do GitLab, o
                # painel não monta nada). `webbrowser.open` é best-effort:
                # em headless retorna False mas não levanta; o clipboard
                # cai como fallback para o operador colar manualmente.
                _open_in_browser(url)
                _copy_to_clipboard(url)
            # Não temos toast ainda — devolve refresh para a próxima render
            # surgir com indicador. (Fase 6: ActionsOverlay com toast queue.)
            return ActionResult.refresh()
        return ActionResult()


def _copy_to_clipboard(text: str) -> bool:
    """Tenta colar `text` no clipboard via pbcopy/xclip/wl-copy.

    Sem erro fatal se nenhum bin disponível — é só um nice-to-have —
    mas registra warning para o operador entender por que copy não fez nada.
    """
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True, timeout=2)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    logger.warning(
        "_copy_to_clipboard: nenhum bin (pbcopy/xclip/wl-copy) encontrado",
    )
    return False


def _open_in_browser(url: str) -> bool:
    """Abre `url` no browser default do OS — best-effort, nunca levanta.

    Usa ``webbrowser.open`` da stdlib (delega para ``open`` no macOS,
    ``xdg-open`` no Linux, ``start`` no Windows). Em ambiente headless
    (CI, container sem DISPLAY) o ``webbrowser`` registra
    ``BROWSER`` vazio e o ``open()`` retorna False; o operador ainda
    tem a URL no clipboard via ``_copy_to_clipboard``. Qualquer exceção
    é capturada e logada em WARNING — abrir browser não é crítico,
    o painel não deve cair por isso.
    """
    if not url:
        return False
    try:
        return bool(webbrowser.open(url, new=2, autoraise=True))
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("_open_in_browser falhou para %s: %s", url, exc)
        return False


class TokensView(View):
    """Detalhe de custos via UsageRepository.

    Mostra breakdown por provider (1h / 24h), records e top 5 sessions.
    A view tem TTL alto (60s) — o SQLite é local mas re-abrir a cada
    poucos segundos é desperdício.
    """

    name = "tokens"
    title = "Tokens & Custos"
    refresh_s = 1.0

    HOTKEYS = "[r] força refresh   [esc] volta   [q] sai"

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data

    def render(self, app: "PanelApp") -> RenderableType:
        if self.data is None:
            body: RenderableType = Text(
                "(modo demo — sem UsageRepository real)", style="dim yellow")
            return self._wrap(app, body)
        c = self.data.costs.get()
        if c.records_24h == 0:
            body = Text(
                "Sem registros de uso nas últimas 24h.\n\n"
                "O UsageRepository fica em ~/.deile/db/usage.db e só recebe\n"
                "dados quando você roda o agente localmente. Os pods em K8s\n"
                "usam o PVC do worker — para o painel ver, monte o PVC ou\n"
                "implemente o endpoint /admin/usage (Fase 9).",
                style="dim",
            )
            return self._wrap(app, body)
        # Tabela por provider
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        tbl.add_column("provider", style="bold cyan")
        tbl.add_column("24h", justify="right", style="bold green")
        tbl.add_column("1h", justify="right", style="green")
        tbl.add_column("%", justify="right", style="dim")
        for prov in sorted(c.by_provider_24h.keys(),
                           key=lambda k: -c.by_provider_24h[k]):
            cost_24 = c.by_provider_24h[prov]
            cost_1 = c.by_provider_1h.get(prov, 0.0)
            pct = (cost_24 / c.total_24h * 100) if c.total_24h else 0
            tbl.add_row(prov, f"${cost_24:.3f}", f"${cost_1:.3f}",
                        f"{pct:5.1f}%")
        tbl.add_row(
            Text("TOTAL", style="bold"),
            Text(f"${c.total_24h:.3f}", style="bold green"),
            Text(f"${c.total_1h:.3f}", style="bold green"),
            "—",
        )
        # Top sessions
        if c.top_sessions_24h:
            sess_tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
            sess_tbl.add_column("session_id", style="dim")
            sess_tbl.add_column("cost", justify="right", style="bold green")
            for sid, cost in c.top_sessions_24h:
                sess_tbl.add_row(sid[:40], f"${cost:.3f}")
        else:
            sess_tbl = Text("· sem sessions registradas", style="dim")
        meta = Text.assemble(
            ("records 24h: ", "dim"),
            (f"{c.records_24h}", "bold"),
            ("   ·   total: ", "dim"),
            (f"${c.total_24h:.2f}", "bold green"),
            ("   ·   1h: ", "dim"),
            (f"${c.total_1h:.2f}", "bold green"),
        )
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(meta, border_style="dim"), name="meta", size=3),
            Layout(Panel(tbl, title="[bold]POR PROVIDER[/bold]",
                         title_align="left", border_style="green"),
                   name="prov"),
            Layout(Panel(sess_tbl, title="[bold]TOP 5 SESSIONS (24h)[/bold]",
                         title_align="left", border_style="blue"),
                   name="sess", size=10),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout

    def _wrap(self, app: "PanelApp", body: RenderableType) -> RenderableType:
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(body, border_style="dim"), name="body"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout


# ----- Notifier echo --------------------------------------------------------

# Fallback regex caso a linha não seja JSON pura — checagem barata
# antes de tentar `json.loads`. JSON-first é mais robusto que regex
# (cobre serializadores que reordenam chaves, espaços etc).
_AUDIT_LOG_RE = re.compile(r'"logger":\s*"deilebot\.audit"')


class NotifierEchoView(View):
    """Últimas mensagens de I/O do bot (audit log).

    Os dados vêm do `BotAuditProvider` (cacheado, refrescado em
    background pelo `BackgroundRefresher`). O render nunca chama
    subprocess — antes da Fase 11 ele fazia `kubectl logs` direto a
    cada 5s e congelava a UI.
    """

    name = "notifier-echo"
    title = "Notifier Echo"
    refresh_s = 1.0

    HOTKEYS = "[r] força refresh   [esc] volta   [q] sai"

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data

    def _fetch_lines(self) -> List[Dict[str, Any]]:
        # Lê do NotifierProvider (cache TTL 5s) — antes fazia kubectl direto
        # por render, gerando 12 chamadas/min. Quando o LocalAuditProvider
        # está ativo (modo local), mescla os eventos do audit-log local
        # com os do pod do bot — assim uma execução híbrida (bot local +
        # k8s pipeline) vê I/O dos dois lados na mesma tela.
        events: List[Dict[str, Any]] = []
        if self.data is not None and self.data.notifier is not None:
            for line in self.data.notifier.get():
                ev = self._parse_line(line)
                if ev is not None:
                    events.append(ev)
        if self.data is not None and self.data.local_audit is not None:
            for ev in self.data.local_audit.get():
                # Marca origem para a tabela mostrar fonte.
                if isinstance(ev, dict):
                    ev = dict(ev)
                    ev.setdefault("_source", "local")
                    events.append(ev)
        # Ordena por `ts` quando presente; eventos sem ts ficam no fim.
        def _sort_key(ev: Dict[str, Any]) -> str:
            return str(ev.get("ts") or ev.get("timestamp") or "")
        events.sort(key=_sort_key)
        return events

    @staticmethod
    def _normalize_event(ev: Dict[str, Any]) -> tuple:
        """Extrai (ts, name, status, detail) de evento k8s/bot OU local.

        Bot/k8s shape:    `{ts, event, payload, ...}`
        Local-audit shape:`{timestamp, event_type, result, actor, details, ...}`
        """
        ts_raw = ev.get("ts") or ev.get("timestamp") or ""
        ts = str(ts_raw)[-19:]
        # Nome: prefere `event` (bot), depois `event_type` (audit local).
        # Audit local complementa com `actor` quando útil (ex:
        # "security_policy_changed [panel:set_preferred_model]").
        name = str(ev.get("event") or ev.get("event_type") or ev.get("message") or "—")
        actor = ev.get("actor")
        if actor and "event" not in ev:
            name = f"{name} [{actor}]"
        # Status:
        # 1) Audit local tem `result` semântico (completed/allowed/denied/failed)
        # 2) Bot infere do nome do evento (sent/received/failed)
        result = ev.get("result")
        if result:
            status = ({"completed": "OK", "allowed": "OK",
                       "denied": "DENY", "cancelled": "DENY",
                       "failed": "FAIL"}.get(str(result).lower(), "—"))
        elif "sent" in name or "received" in name:
            status = "OK"
        elif "failed" in name:
            status = "FAIL"
        else:
            status = "—"
        # Detail: tenta `payload` (bot) ou `details` (audit local) ou
        # cai em `resource` (audit local quando details está vazio).
        payload = ev.get("payload") or ev.get("details") or {}
        if payload and isinstance(payload, dict):
            detail = ", ".join(f"{k}={v}" for k, v in payload.items())
        else:
            detail = str(ev.get("resource") or "")
        return ts, name, status, detail

    @staticmethod
    def _parse_line(line: str) -> Optional[Dict[str, Any]]:
        """Tenta JSON primeiro (estrutura do audit do bot); fallback ao regex.

        Estruturas diferentes de log (com `{...}` wrappados em prefixos do
        runtime do bot) caem no regex match + json no payload."""
        line = line.strip()
        if not line:
            return None
        # JSON puro
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("logger") == "deilebot.audit":
                    return obj
            except json.JSONDecodeError:
                pass
        # Fallback: regex casa qualquer linha contendo logger=deilebot.audit
        # e tenta extrair o último JSON da linha.
        if not _AUDIT_LOG_RE.search(line):
            return None
        brace = line.find("{")
        if brace < 0:
            return None
        try:
            return json.loads(line[brace:])
        except json.JSONDecodeError:
            return None

    def render(self, app: "PanelApp") -> RenderableType:
        # Renderiza eventos vindos de DUAS fontes (campos diferentes):
        # - k8s/bot audit: `ts`, `event`, `payload`, status implícito no
        #   nome do evento (`sent`/`received`/`failed`)
        # - local audit (security_audit.log): `timestamp`, `event_type`,
        #   `result` (`completed`/`allowed`/`denied`/`failed`), `details`,
        #   `actor` (e.g. `panel:set_preferred_model`)
        # `_normalize_event` produz uma tupla uniforme (ts, name, status,
        # detail) que cobre ambos sem perder informação.
        events = self._fetch_lines()
        if not events:
            body: RenderableType = Text(
                "Nenhum evento de audit recente.\n\n"
                "Aparece aqui: I/O do bot (DM enviada/recebida) E qualquer\n"
                "ação privilegiada do painel local (trocar modelo, ações\n"
                "destrutivas), via ~/.deile/logs/security_audit.log.",
                style="dim",
            )
        else:
            tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
            tbl.add_column("ts", width=20, style="dim")
            tbl.add_column("event", width=26, style="bold")
            tbl.add_column("status", width=10)
            tbl.add_column("detail")
            for ev in events[-20:]:
                ts, name, status, detail = self._normalize_event(ev)
                ok_style = ("green" if status == "OK"
                            else "red" if status == "FAIL"
                            else "yellow" if status == "DENY"
                            else "dim")
                tbl.add_row(ts, name, Text(status, style=f"bold {ok_style}"),
                            Text(detail[:60], style="dim"))
            body = tbl
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(body, title="[bold]AUDIT EVENTS[/bold]",
                         title_align="left", border_style="cyan"),
                   name="body"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout


# ----- Actions overlay ------------------------------------------------------

@dataclass
class _ActionSpec:
    label: str
    cmd: List[str]
    destructive: bool = False
    # Verbo "mutador" = modifica estado do cluster (build/up/restart/stop/
    # start/test/down). Read-only (`status`) tem mutates=False e roda sem
    # confirmação. Todos os mutadores exigem [y/N] mesmo quando não
    # destrutivos — README anuncia "não muta nada do estado existente".
    mutates: bool = False
    # Verbo curto pra emitir como `action` no AuditEvent — ergonômico pra
    # filtragem no log (e.g., `action=k8s_restart`). Derivado por padrão
    # da segunda palavra do `cmd` (e.g., `[..., "k8s", "restart", ...]`).
    audit_action: Optional[str] = None

    def resolved_audit_action(self) -> str:
        if self.audit_action:
            return self.audit_action
        # cmd costuma ser [py, deploy, "k8s", "<verb>", ...] — pega o verb.
        try:
            k8s_idx = self.cmd.index("k8s")
            return f"k8s_{self.cmd[k8s_idx + 1]}"
        except (ValueError, IndexError):
            return "dispatch"


def _audit_panel_action(spec: "_ActionSpec", *, result: str,
                        detail: str = "") -> None:
    """Emite AuditEvent(TOOL_EXECUTION) para uma ação do painel.

    Falha silenciosa se o pacote `deile` não estiver importável (rodando
    isolado) — mas registra warning no logger local."""
    try:
        from deile.security.audit_logger import (  # noqa: PLC0415
            AuditEventType, SeverityLevel, get_audit_logger)
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit logger indisponível para ação do painel: %s", exc)
        return
    severity = (SeverityLevel.WARNING if spec.destructive
                else SeverityLevel.INFO)
    try:
        get_audit_logger().log_event(
            event_type=AuditEventType.TOOL_EXECUTION,
            severity=severity,
            actor="panel:actions",
            resource=f"deploy.py:{spec.label}",
            action=spec.resolved_audit_action(),
            result=result,
            details={
                "label": spec.label,
                "cmd": spec.cmd,
                "destructive": spec.destructive,
                "mutates": spec.mutates,
                "detail": detail[:200],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("falha emitindo AuditEvent para ação: %s", exc)


class _ActionRunner:
    """Wrap de subprocess streaming pra ActionsView.

    Diferente do _LogStreamer (que vive enquanto a view existe), este é
    one-shot: roda o comando, encerra, deixa o output no buffer pra
    consulta. Cancelável com .stop().
    """

    def __init__(self, cmd: List[str], maxlen: int = 500):
        self.cmd = cmd
        self.buf: Deque[str] = deque(maxlen=maxlen)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self.returncode: Optional[int] = None
        self._stop = threading.Event()

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except OSError as exc:
            self.buf.append(f"[ERRO] {exc}")
            self.returncode = -1
            return
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            # Checa stop ANTES de processar cada linha — sem isso o stop
            # só toma efeito após o próximo flush do stdout do subprocess.
            if self._stop.is_set():
                break
            self.buf.append(line.rstrip())
        self._proc.wait()
        self.returncode = self._proc.returncode
        self.buf.append(f"--- exit {self.returncode} ---")

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=0.5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


class ActionsView(View):
    """Lista de verbos do deploy.py acionáveis a partir do painel."""

    name = "actions"
    title = "Ações"
    refresh_s = 1.0

    HOTKEYS = "[1-9] dispara   [c] cancelar   [esc] volta   [q] sai"

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.runner: Optional[_ActionRunner] = None
        self.last_action: str = ""
        self.confirm_for: Optional[_ActionSpec] = None

    def _actions(self) -> List[_ActionSpec]:
        """Ações disponíveis no contexto vigente.

        K8s: lista completa quando `k8s_available`; apenas `status` (a
        única não-mutadora) quando cluster ausente — evita oferecer
        `restart`/`down`/etc que falhariam sem cluster.

        Locais: aparecem quando `context.local_available` — operações de
        inspeção (tail/open dir/ps). Não-mutadoras por padrão.

        Limite prático de 9 itens visíveis (hotkeys [1-9] no
        `handle_key`); o conjunto k8s+local cabe naturalmente porque um
        deles é reduzido quando o outro não está disponível.
        """
        repo_root = str(Path(__file__).resolve().parent.parent.parent)
        py = sys.executable
        deploy = f"{repo_root}/infra/k8s/deploy.py"
        k8s_on = (self.data is not None and self.data.context is not None
                  and self.data.context.k8s_available)
        # `--yes` pula confirmação interna do deploy.py — a confirmação aqui
        # é nossa, na view, antes de chamar.
        if k8s_on:
            actions: List[_ActionSpec] = [
                _ActionSpec("status (lê cluster)",            [py, deploy, "k8s", "status"]),
                _ActionSpec("restart pods (mesma imagem)",    [py, deploy, "k8s", "restart", "--yes"], mutates=True),
                _ActionSpec("build imagem (pods ficam antigos)", [py, deploy, "k8s", "build", "--yes"], mutates=True),
                _ActionSpec("build + restart pods (deploy)",  [py, deploy, "k8s", "build", "--restart", "--yes"], mutates=True),
                _ActionSpec("up (cria ns + Secrets + Deploys)", [py, deploy, "k8s", "up", "--yes"], mutates=True),
                _ActionSpec("stop (scale 0; mantém PVC)",     [py, deploy, "k8s", "stop", "--yes"], mutates=True),
                _ActionSpec("start (scale 1; retoma)",        [py, deploy, "k8s", "start", "--yes"], mutates=True),
                _ActionSpec("test (Job one-shot → DM)",       [py, deploy, "k8s", "test", "--yes"], mutates=True),
                _ActionSpec("DOWN (apaga ns inteiro)",        [py, deploy, "k8s", "down", "--yes"], destructive=True, mutates=True),
            ]
        else:
            # Sem k8s: só status (que dirá "cluster inacessível" — útil
            # como verificação rápida); resto fica oculto.
            actions = [
                _ActionSpec("status (k8s offline)", [py, deploy, "k8s", "status"]),
            ]
        actions.extend(self._local_actions())
        return actions[:9]  # respeita o limite de hotkeys [1-9]

    def _local_actions(self) -> List[_ActionSpec]:
        """Ações local-mode — só listadas quando há contexto/local disponível."""
        if (self.data is None or self.data.context is None
                or not self.data.context.local_available):
            return []
        ctx = self.data.context
        opener = shutil.which("open") or shutil.which("xdg-open")
        tail = shutil.which("tail")
        ps = shutil.which("ps")
        actions: List[_ActionSpec] = []
        if tail is not None:
            actions.append(_ActionSpec(
                "[local] tail deile.log (n=80)",
                [tail, "-n", "80", str(ctx.logs_dir / "deile.log")],
                audit_action="local_tail_log",
            ))
            actions.append(_ActionSpec(
                "[local] tail security_audit (n=40)",
                [tail, "-n", "40", str(ctx.logs_dir / "security_audit.log")],
                audit_action="local_tail_audit",
            ))
        if ps is not None:
            actions.append(_ActionSpec(
                "[local] ps deile-like",
                [ps, "-axo", "pid,pcpu,rss,etime,command"],
                audit_action="local_ps",
            ))
        if opener is not None:
            actions.append(_ActionSpec(
                "[local] open logs dir",
                [opener, str(ctx.logs_dir)],
                audit_action="local_open_logs",
            ))
            actions.append(_ActionSpec(
                "[local] open sessions dir",
                [opener, str(ctx.sessions_dir)],
                audit_action="local_open_sessions",
            ))
        return actions

    def render(self, app: "PanelApp") -> RenderableType:
        actions = self._actions()
        # menu
        menu = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        menu.add_column(" ", width=3)
        menu.add_column("ação", style="bold")
        menu.add_column("comando", style="dim")
        for i, a in enumerate(actions, 1):
            label = Text(a.label, style="bold red" if a.destructive else "bold")
            menu.add_row(str(i), label, " ".join(a.cmd[-3:]))
        # confirmação?
        if self.confirm_for is not None:
            is_destructive = self.confirm_for.destructive
            tag = "DESTRUTIVA" if is_destructive else "mutadora"
            heading_style = "bold red" if is_destructive else "bold yellow"
            border = "red" if is_destructive else "yellow"
            confirm_body = Group(
                Text(f"Vai rodar a ação {tag}: {self.confirm_for.label}",
                     style=heading_style),
                Text(" ".join(self.confirm_for.cmd), style="dim"),
                Text(),
                Text("Confirma?  [y/N]", style="bold yellow"),
            )
            confirm_panel: Optional[RenderableType] = Panel(
                confirm_body,
                title=f"[{heading_style}]CONFIRMAÇÃO[/{heading_style}]",
                title_align="left", border_style=border,
            )
        else:
            confirm_panel = None
        # output do runner
        if self.runner is not None:
            lines = list(self.runner.buf)[-25:]
            running_label = ("RUNNING" if self.runner.running
                             else f"DONE (exit {self.runner.returncode})")
            color = ("yellow" if self.runner.running
                     else "green" if self.runner.returncode == 0
                     else "red")
            output_body: RenderableType = (
                Text("\n".join(lines), no_wrap=False) if lines
                else Text("(aguardando output)", style="dim")
            )
            output_panel = Panel(
                output_body,
                title=f"[bold]OUTPUT[/bold] · {self.last_action} · {running_label}",
                title_align="left", border_style=color,
            )
        else:
            output_panel = Panel(
                Text("(rode uma ação para ver o output aqui)", style="dim"),
                title="[bold]OUTPUT[/bold]", title_align="left",
                border_style="dim",
            )
        # Subtitle do Panel mostra o ROOT que `build` vai empacotar e a
        # tag de imagem destino. Sempre visível — desambigua "qual versão
        # vai pro k8s?" quando o painel roda numa worktree.
        try:
            repo_root_for_build = Path(__file__).resolve().parent.parent.parent
            subtitle = (
                f"[dim]build empacota: {repo_root_for_build}  →  "
                f"imagem: deile-stack:local (imagePullPolicy: Never)[/dim]"
            )
        except Exception:
            subtitle = None
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(menu, title="[bold]AÇÕES[/bold]",
                         title_align="left", border_style="cyan",
                         subtitle=subtitle, subtitle_align="left"),
                   name="menu", size=14),
            *([Layout(confirm_panel, name="confirm", size=6)]
              if confirm_panel is not None else []),
            Layout(output_panel, name="output"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        if self.confirm_for is not None:
            if key == "y":
                self._dispatch(self.confirm_for)
                self.confirm_for = None
                return ActionResult.refresh()
            # Default-deny: qualquer tecla que não seja 'y' (incluindo
            # 'n' e Enter) cancela. Mantém prompt no estilo `[y/N]`.
            _audit_panel_action(
                self.confirm_for, result="denied",
                detail="operador cancelou na confirmação",
            )
            self.confirm_for = None
            return ActionResult.refresh()
        if key == "c" and self.runner is not None and self.runner.running:
            self.runner.stop()
            return ActionResult.refresh()
        if key.isdigit():
            idx = int(key) - 1
            actions = self._actions()
            if 0 <= idx < len(actions):
                spec = actions[idx]
                # Toda ação mutadora (mutates=True) — destrutiva ou não —
                # precisa de confirmação explícita do operador. README anuncia
                # "não muta nada do estado existente" sem opt-in; o gate é o
                # `[y/N]`. `status` (não-mutador) roda direto.
                if spec.mutates:
                    self.confirm_for = spec
                else:
                    self._dispatch(spec)
                return ActionResult.refresh()
        return ActionResult()

    def _dispatch(self, spec: _ActionSpec) -> None:
        if self.runner is not None and self.runner.running:
            _audit_panel_action(
                spec, result="denied",
                detail="já tem ação rodando — ignorado",
            )
            return                # já tem ação rodando, ignora
        # Audit ANTES de iniciar — registra a intenção mesmo que o
        # subprocess falhe ao ligar.
        _audit_panel_action(spec, result="allowed",
                            detail="iniciando subprocess")
        self.runner = _ActionRunner(spec.cmd)
        self.last_action = spec.label
        self.runner.start()

    def on_unmount(self, app: "PanelApp") -> None:
        # Mata runner pendente quando o usuário sai da view (ESC).
        if self.runner is not None and self.runner.running:
            self.runner.stop()


class ModelSwitcherView(View):
    """Troca o `DEILE_PREFERRED_MODEL` do worker (e/ou pipeline) em runtime.

    Implementação **opção A** (env var + rollout): `kubectl set env` muda
    a spec do Deployment e dispara rollout. No worker (RollingUpdate
    maxSurge:1/maxUnavailable:0) o swap é zero-downtime. Tarefas in-flight
    rodam até o fim antes do pod morrer.

    Opções B (endpoint /admin/set_model no worker, swap instantâneo) e C
    (ConfigMap + hot-reload via watchdog) ficam como evolução futura —
    exigem código no worker_server e no settings/watchdog respectivamente.
    """

    name = "model-switcher"
    title = "Trocar modelo (runtime)"
    refresh_s = 1.0

    HOTKEYS = ("[↑/↓] navega   [w] target=worker   [l] target=pipeline   "
               "[b] both   [enter] aplicar   [esc] volta   [q] sai")

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.cursor: int = 0
        self.target: str = "worker"  # 'worker' | 'pipeline' | 'both'
        # Estado da última troca (mostrado no painel até a próxima).
        self.last_msg: str = ""
        self.last_ok: Optional[bool] = None
        self.confirm_for: Optional[str] = None  # slug em confirmação

    def _models(self):
        if self.data is None:
            return []
        return self.data.models.get()

    def _current(self) -> Dict[str, Optional[str]]:
        if self.data is None:
            return {}
        return self.data.current_model.get()

    def render(self, app: "PanelApp") -> RenderableType:
        models = self._models()
        current = self._current()

        # Sem k8s, troca via `kubectl set env` não roda — mostra aviso
        # honesto em vez de prometer ação que falharia silenciosa.
        k8s_on = (self.data is not None and self.data.context is not None
                  and self.data.context.k8s_available)
        if not k8s_on and self.data is not None:
            body = Group(
                Align.center(Text("Modelo de runtime — só k8s",
                                  style="bold yellow")),
                Text(),
                Align.center(Text(
                    "A troca usa `kubectl set env deploy/...` (rollout).",
                    style="dim")),
                Align.center(Text(
                    "Em modo local, ajuste o modelo via `model_providers.yaml`",
                    style="dim")),
                Align.center(Text(
                    "ou via `DEILE_PREFERRED_MODEL` no seu shell.",
                    style="dim")),
                Text(),
                Align.center(Text("[esc] volta", style="dim")),
            )
            layout = Layout()
            layout.split_column(
                Layout(_head_panel(self.title, app), name="head", size=4),
                Layout(Panel(body, border_style="dim"), name="body"),
                Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
            )
            return layout

        # Header com targets + valor atual
        target_pretty = {"worker": "deile-worker",
                         "pipeline": "deile-pipeline",
                         "both": "deile-worker + deile-pipeline"}[self.target]
        head_lines = [
            Text.assemble(
                ("target: ", "dim"),
                (target_pretty, "bold yellow"),
            ),
            Text.assemble(
                ("DEILE_PREFERRED_MODEL atual:", "dim"),
            ),
        ]
        for dep, val in current.items():
            head_lines.append(Text.assemble(
                ("  · ", "dim"),
                (f"{dep}: ", "bold"),
                (val or "(não setado — usa defaults do settings)",
                 "cyan" if val else "dim yellow"),
            ))
        header_panel = Panel(Group(*head_lines),
                             title="[bold]TARGET[/bold]",
                             title_align="left", border_style="yellow")

        # Tabela de modelos
        if not models:
            body: RenderableType = Text(
                "(sem modelos no catálogo — checar model_providers.yaml)",
                style="dim")
            list_panel = Panel(body, title="[bold]MODELOS[/bold]",
                               title_align="left", border_style="dim")
        else:
            self.cursor = max(0, min(self.cursor, len(models) - 1))
            tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
            tbl.add_column(" ", width=2)
            tbl.add_column("slug", style="bold")
            tbl.add_column("display", style="cyan")
            tbl.add_column("tier", width=8)
            tbl.add_column("label", width=10)
            tbl.add_column("$/1M in", justify="right", style="green")
            tbl.add_column("$/1M out", justify="right", style="green")
            current_vals = set(v for v in current.values() if v)
            for i, m in enumerate(models):
                marker = "▶" if i == self.cursor else " "
                is_active = m.slug in current_vals
                slug_text = Text(
                    m.slug,
                    style="bold yellow" if is_active else "bold",
                )
                if is_active:
                    slug_text.append("  ●", style="bold green")
                tbl.add_row(
                    Text(marker, style="bold cyan"),
                    slug_text,
                    m.display_name,
                    m.tier,
                    m.label,
                    f"${m.input_cost_per_1m:.2f}",
                    f"${m.output_cost_per_1m:.2f}",
                )
            list_panel = Panel(tbl, title="[bold]MODELOS DISPONÍVEIS[/bold]",
                               title_align="left", border_style="cyan")

        # Painel de status / confirmação
        if self.confirm_for is not None:
            confirm_panel: Optional[RenderableType] = Panel(
                Group(
                    Text(f"Aplicar {self.confirm_for} em {target_pretty}?",
                         style="bold yellow"),
                    Text("`kubectl set env` dispara rollout automático "
                         "(zero-downtime no worker).", style="dim"),
                    Text(),
                    Text("[y] confirmar    [n] cancelar", style="bold cyan"),
                ),
                title="[bold yellow]CONFIRMAÇÃO[/bold yellow]",
                title_align="left", border_style="yellow",
            )
        elif self.last_msg:
            border = "green" if self.last_ok else "red"
            confirm_panel = Panel(
                Text(self.last_msg, style="bold green" if self.last_ok
                     else "bold red"),
                title="[bold]ÚLTIMA AÇÃO[/bold]",
                title_align="left", border_style=border,
            )
        else:
            confirm_panel = None

        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(header_panel, name="targets", size=8),
            Layout(list_panel, name="list"),
            *([Layout(confirm_panel, name="confirm", size=7)]
              if confirm_panel is not None else []),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        # Resolver confirmação primeiro: na pendência, default-deny (só [y]
        # aplica; qualquer outra tecla cancela e emite audit). Casa o
        # padrão de ActionsView e evita confirmação ficar pendurada.
        if self.confirm_for is not None:
            slug = self.confirm_for
            if key == "y":
                self._apply(slug)
                self.confirm_for = None
                return ActionResult.refresh()
            reason = ("operador cancelou na confirmação" if key == "n"
                      else f"tecla inesperada na confirmação: {key!r}")
            pd_audit_security_policy_change(
                self.target, slug, result="cancelled", detail=reason,
            )
            self.confirm_for = None
            self.last_msg = "cancelado pelo operador"
            self.last_ok = False
            return ActionResult.refresh()
        # Troca de target. Note: 'p' é shadowed pelo pause/resume global —
        # usamos 'l' (pipe-L-ine) para o target pipeline.
        if key == "w":
            self.target = "worker"
            return ActionResult.refresh()
        if key == "l":
            self.target = "pipeline"
            return ActionResult.refresh()
        if key == "b":
            self.target = "both"
            return ActionResult.refresh()
        models = self._models()
        n = len(models)
        if n == 0:
            return ActionResult()
        if key in ("UP", "k"):
            self.cursor = (self.cursor - 1) % n
            return ActionResult.refresh()
        if key in ("DOWN", "j"):
            self.cursor = (self.cursor + 1) % n
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            self.confirm_for = models[self.cursor].slug
            return ActionResult.refresh()
        return ActionResult()

    def _apply(self, slug: str) -> None:
        if self.data is None:
            self.last_msg = "modo demo — nenhuma ação aplicada"
            self.last_ok = False
            return
        deployments = (("deile-worker",) if self.target == "worker"
                       else ("deile-pipeline",) if self.target == "pipeline"
                       else ("deile-worker", "deile-pipeline"))
        # Para rollback em "both": captura o slug ATUAL de cada deployment
        # ANTES de tentar trocar. Se o segundo falhar depois do primeiro
        # ter sucesso, tentamos reverter o primeiro ao valor capturado.
        # `force=True` evita usar um snapshot do cache (até 5s de idade) —
        # o estado real do cluster pode ter mudado desde a última leitura.
        prev_slugs: Dict[str, Optional[str]] = {}
        if len(deployments) > 1:
            try:
                current_snap = self.data.current_model.get(force=True)
            except Exception:  # noqa: BLE001
                current_snap = {}
            for dep in deployments:
                prev_slugs[dep] = current_snap.get(dep)

        ns = self.data.context.namespace if self.data.context else _NS_DEFAULT
        results: List[tuple] = []
        rolled_back: List[str] = []
        for dep in deployments:
            ok, msg = pd_set_preferred_model(dep, slug, namespace=ns)
            results.append((dep, ok, msg))
            # `len(deployments) > 1` é implícito: para `len == 1`,
            # `results[:-1]` é sempre vazio, logo `any(...)` é False.
            if not ok and any(
                ok_prev for _, ok_prev, _ in results[:-1]
            ):
                # Tenta reverter os deployments já alterados (best-effort).
                for prev_dep, prev_ok, _ in results[:-1]:
                    if not prev_ok:
                        continue
                    prev = prev_slugs.get(prev_dep)
                    if not prev:
                        # Sem slug anterior conhecido: não consegue reverter
                        # (era default-do-settings). Registra no alerta final.
                        rolled_back.append(f"{prev_dep}:sem-prev")
                        continue
                    rb_ok, rb_msg = pd_set_preferred_model(prev_dep, prev, namespace=ns)
                    status = "rb-ok" if rb_ok else "rb-fail"
                    # Inclui prefixo curto do output (até 30 chars) — útil
                    # pra distinguir "kubectl ausente" de "permission denied"
                    # sem precisar abrir o audit log.
                    snippet = (rb_msg or "").strip()[:30]
                    rolled_back.append(
                        f"{prev_dep}:{status}"
                        + (f" ({snippet})" if snippet else "")
                    )
                break  # não tenta o resto da lista após falha+rollback
        all_ok = all(ok for _, ok, _ in results) and len(results) == len(deployments)
        # Força provider a re-ler o valor atual no próximo render.
        self.data.current_model._cache.invalidate()  # noqa: SLF001
        self.last_ok = all_ok
        msg = " | ".join(
            f"{dep}: {'OK' if ok else 'FAIL'} ({m[:40]})"
            for dep, ok, m in results
        )
        if rolled_back:
            msg += f"  | rollback: {', '.join(rolled_back)}"
        self.last_msg = msg


class StageModelsView(View):
    """Per-stage model override editor (issue #305) — layout dinâmico.

    Cada uma das 5 etapas do pipeline (``classify`` / ``refine`` /
    ``implement`` / ``pr_review`` / ``follow_ups``) pode ter um modelo
    diferente. Etapas sem override caem no ``DEILE_PREFERRED_MODEL`` global.

    A view se adapta à largura do terminal:
      - ``width < 100``: tabela colapsada (Etapa + Efetivo só);
      - ``100 ≤ width < 140``: tabela completa (Etapa + Override + Efetivo);
      - ``width ≥ 140``: tabela completa + painel lateral com o catálogo
        do ``ModelsProvider`` para edição visual.

    Persistência via ``set_stage_model`` (settings.json + audit log); o
    worker pega a próxima dispatch já com o modelo novo (sem rollout).
    """

    name = "stage-models"
    title = "Modelos por etapa do pipeline (settings.json)"
    refresh_s = 1.0

    HOTKEYS = ("[↑/↓] navega   [enter] editar   [c] limpar override   "
               "[r] refresh   [esc] volta   [q] sai")

    # Source of truth for the row count — keep in sync with PIPELINE_STAGES.
    # We list it here (and not import at class scope) so the view stays
    # importable even when the deile package is not on sys.path (panel runs
    # from infra/k8s in some installs).
    _STAGES_FALLBACK = ("classify", "refine", "implement", "pr_review",
                        "follow_ups")

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.cursor: int = 0
        self.last_msg: str = ""
        self.last_ok: Optional[bool] = None
        # Edit modal state: None = browsing; ("set", stage) = picking model
        # for stage; ("clear", stage) = confirming clear.
        self.mode: Optional[tuple] = None
        self.picker_cursor: int = 0

    def on_unmount(self, app: "PanelApp") -> None:
        # Re-entry should always land on the stage list, never on a stale
        # picker modal that the operator dismissed by leaving the view.
        self.mode = None
        self.picker_cursor = 0

    def intercepts_key(self, key: str) -> bool:
        # ESC inside a modal must close the modal (handled by our own
        # _handle_picker_key / _handle_clear_confirm_key), not pop the view.
        return key == "ESC" and self.mode is not None

    # --- data accessors --------------------------------------------------

    def _entries(self) -> List:
        if self.data is None:
            # Demo mode — synthesise 5 fallback rows so the layout still
            # renders. Effective = "(demo)" to make the mode visible.
            from _panel_data import StageModelEntry  # noqa: PLC0415
            return [
                StageModelEntry(stage=s, override=None,
                                effective="(demo)", is_fallback=True)
                for s in self._STAGES_FALLBACK
            ]
        return self.data.stage_models.get()

    def _models(self):
        if self.data is None:
            return []
        return self.data.models.get()

    # --- rendering -------------------------------------------------------

    def _render_table(self, entries: List, width: int) -> RenderableType:
        """Render the stage table; columns adapt to *width*.

        Three breakpoints chosen to match common terminal sizes (80, 120,
        160 cols). The narrowest path drops the Override column entirely;
        the widest one also drops nothing but lets the wide-layout add a
        sibling panel beside this one.
        """
        compact = width < 100
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        tbl.add_column("#", width=2, justify="right")
        tbl.add_column("Etapa", style="bold")
        if not compact:
            tbl.add_column("Override", style="cyan", overflow="fold")
        tbl.add_column("Efetivo", style="green", overflow="fold")
        if not compact:
            tbl.add_column(" ", width=2)
        n = len(entries)
        if n == 0:
            tbl.add_row(*(["—"] * len(tbl.columns)))
            return Panel(tbl, title="[bold]ETAPAS[/bold]",
                         title_align="left", border_style="cyan")
        self.cursor = max(0, min(self.cursor, n - 1))
        for i, e in enumerate(entries):
            marker = "▶" if i == self.cursor else " "
            override_txt = e.override or "(não setado)"
            effective_txt = e.effective or "(nenhum — agente usa default)"
            fallback_mark = "◄" if e.is_fallback else ""
            row = [
                Text(f"{marker}{i + 1}", style="bold cyan"),
                Text(e.stage),
            ]
            if not compact:
                row.append(Text(override_txt,
                                style="dim" if not e.override else "cyan"))
            row.append(Text(effective_txt,
                            style="green" if e.effective else "dim"))
            if not compact:
                row.append(Text(fallback_mark, style="dim yellow"))
            tbl.add_row(*row)
        legend = ("◄ = sem override per-stage; herda DEILE_PREFERRED_MODEL"
                  if not compact else "(modo estreito: tabela colapsada)")
        return Panel(
            Group(tbl, Text(legend, style="dim")),
            title="[bold]MODELOS POR ETAPA[/bold]",
            title_align="left", border_style="cyan",
        )

    def _render_picker(self, stage: str, width: int) -> RenderableType:
        """Picker panel: cataloga modelos para seleção (modo 'set')."""
        models = self._models()
        compact = width < 100
        if not models:
            return Panel(
                Text("(sem modelos no catálogo — checar model_providers.yaml)",
                     style="dim"),
                title=f"[bold yellow]ESCOLHA UM MODELO PARA '{stage}'[/bold yellow]",
                title_align="left", border_style="yellow",
            )
        self.picker_cursor = max(0, min(self.picker_cursor, len(models) - 1))
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        tbl.add_column(" ", width=2)
        tbl.add_column("slug", style="bold")
        if not compact:
            tbl.add_column("display", style="cyan")
            tbl.add_column("$/1M in", justify="right", style="green")
            tbl.add_column("$/1M out", justify="right", style="green")
        for i, m in enumerate(models):
            marker = "▶" if i == self.picker_cursor else " "
            row = [Text(marker, style="bold cyan"), Text(m.slug, style="bold")]
            if not compact:
                row.extend([
                    Text(m.display_name),
                    Text(f"${m.input_cost_per_1m:.2f}"),
                    Text(f"${m.output_cost_per_1m:.2f}"),
                ])
            tbl.add_row(*row)
        return Panel(
            Group(
                tbl,
                Text("[↑/↓] navega   [enter] confirma   [esc] cancela",
                     style="dim cyan"),
            ),
            title=f"[bold yellow]ESCOLHA UM MODELO PARA '{stage}'[/bold yellow]",
            title_align="left", border_style="yellow",
        )

    def _render_status(self) -> Optional[RenderableType]:
        if not self.last_msg:
            return None
        border = "green" if self.last_ok else "red"
        return Panel(
            Text(self.last_msg,
                 style="bold green" if self.last_ok else "bold red"),
            title="[bold]ÚLTIMA AÇÃO[/bold]",
            title_align="left", border_style=border,
        )

    def render(self, app: "PanelApp") -> RenderableType:
        entries = self._entries()
        width = app.console.size.width or 100

        layout = Layout()
        sections: List[Layout] = [
            Layout(_head_panel(self.title, app), name="head", size=4),
        ]

        # Modal picker / confirmation on top of the body.
        if self.mode is not None and self.mode[0] == "set":
            sections.append(Layout(self._render_picker(self.mode[1], width),
                                   name="picker"))
        elif self.mode is not None and self.mode[0] == "clear":
            sections.append(Layout(Panel(
                Group(
                    Text(f"Limpar override de '{self.mode[1]}'?",
                         style="bold yellow"),
                    Text("A etapa voltará a usar o DEILE_PREFERRED_MODEL "
                         "global na próxima dispatch.", style="dim"),
                    Text(),
                    Text("[y] confirmar    [n] cancelar", style="bold cyan"),
                ),
                title="[bold yellow]CONFIRMAR LIMPEZA[/bold yellow]",
                title_align="left", border_style="yellow",
            ), name="confirm", size=8))
        else:
            # Browsing mode — adaptive body. Wide layouts split table +
            # catalog side-by-side (a visual cue that picker is keyboard-ready
            # without leaving this view).
            table_panel = self._render_table(entries, width)
            if width >= 140 and self._models():
                catalog_tbl = Table(box=box.SIMPLE_HEAD, expand=True)
                catalog_tbl.add_column("slug", style="bold")
                catalog_tbl.add_column("tier", width=8)
                catalog_tbl.add_column("$/1M in", justify="right",
                                       style="green")
                catalog_tbl.add_column("$/1M out", justify="right",
                                       style="green")
                for m in self._models():
                    catalog_tbl.add_row(m.slug, m.tier,
                                        f"${m.input_cost_per_1m:.2f}",
                                        f"${m.output_cost_per_1m:.2f}")
                catalog_panel = Panel(
                    catalog_tbl,
                    title="[bold]CATÁLOGO[/bold]",
                    title_align="left", border_style="dim",
                )
                body = Layout(name="body")
                body.split_row(
                    Layout(table_panel, name="table", ratio=2),
                    Layout(catalog_panel, name="catalog", ratio=1),
                )
                sections.append(body)
            else:
                sections.append(Layout(table_panel, name="body"))

        status_panel = self._render_status()
        if status_panel is not None:
            sections.append(Layout(status_panel, name="status", size=4))

        sections.append(Layout(_footer_panel(self.HOTKEYS),
                               name="footer", size=3))
        layout.split_column(*sections)
        return layout

    # --- input -----------------------------------------------------------

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        # Picker modal: navigate + confirm/cancel.
        if self.mode is not None and self.mode[0] == "set":
            return self._handle_picker_key(key)
        # Clear-confirmation modal.
        if self.mode is not None and self.mode[0] == "clear":
            return self._handle_clear_confirm_key(key)
        # Browsing.
        entries = self._entries()
        n = len(entries)
        if n == 0:
            return ActionResult()
        if key in ("UP", "k"):
            self.cursor = (self.cursor - 1) % n
            return ActionResult.refresh()
        if key in ("DOWN", "j"):
            self.cursor = (self.cursor + 1) % n
            return ActionResult.refresh()
        # Number shortcut: 1-5 jump to that row.
        if key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < n:
                self.cursor = idx
                return ActionResult.refresh()
        if key in ("\r", "\n"):
            self.mode = ("set", entries[self.cursor].stage)
            self.picker_cursor = 0
            return ActionResult.refresh()
        if key == "c":
            self.mode = ("clear", entries[self.cursor].stage)
            return ActionResult.refresh()
        return ActionResult()

    def _handle_picker_key(self, key: str) -> ActionResult:
        models = self._models()
        if key == "ESC":
            self.mode = None
            return ActionResult.refresh()
        if not models:
            if key in ("\r", "\n"):
                self.mode = None
            return ActionResult()
        if key in ("UP", "k"):
            self.picker_cursor = (self.picker_cursor - 1) % len(models)
            return ActionResult.refresh()
        if key in ("DOWN", "j"):
            self.picker_cursor = (self.picker_cursor + 1) % len(models)
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            stage = self.mode[1] if self.mode else ""
            slug = models[self.picker_cursor].slug
            self._apply_set(stage, slug)
            self.mode = None
            if self.data is not None:
                self.data.stage_models._cache.invalidate()  # noqa: SLF001
            return ActionResult.refresh()
        return ActionResult()

    def _handle_clear_confirm_key(self, key: str) -> ActionResult:
        stage = self.mode[1] if self.mode else ""
        if key == "y":
            self._apply_clear(stage)
            self.mode = None
            if self.data is not None:
                self.data.stage_models._cache.invalidate()  # noqa: SLF001
            return ActionResult.refresh()
        # ESC / n / anything else cancels.
        self.mode = None
        self.last_msg = "cancelado pelo operador"
        self.last_ok = False
        return ActionResult.refresh()

    # --- writes ----------------------------------------------------------

    def _apply_set(self, stage: str, slug: str) -> None:
        if self.data is None:
            self.last_msg = "modo demo — nenhuma escrita aplicada"
            self.last_ok = False
            return
        ok, msg = pd_set_stage_model(stage, slug)
        self.last_ok = ok
        self.last_msg = msg

    def _apply_clear(self, stage: str) -> None:
        if self.data is None:
            self.last_msg = "modo demo — nenhuma escrita aplicada"
            self.last_ok = False
            return
        ok, msg = pd_clear_stage_model(stage)
        self.last_ok = ok
        self.last_msg = msg


class DispatchModeView(View):
    """Editor do dispatch mode do pipeline (issue #309) — layout dinâmico.

    O ``PipelineMonitor`` instancia ``ClaudeImplementer`` (``claude -p`` num
    worktree local) ou ``WorkerImplementer`` (HTTP → deile-worker), decidido por
    ``settings.pipeline_dispatch_mode``. A view permite flipar entre os dois
    sem editar manifest ou ConfigMap: ``kubectl set env`` sobre
    ``deile-pipeline`` dispara rollout (strategy ``Recreate``) e a próxima
    dispatch usa o modo novo.

    Limitação operacional: setar ``claude`` só funciona se o binary ``claude``
    estiver no PATH dentro do pod E houver credentials montadas para
    ``~/.claude/``. Hoje o image NÃO instala o ``claude`` CLI nem monta
    credentials — a próxima dispatch falha com um erro `claude binary not
    found` claro (sem dano além disso). Issue de follow-up cobre o trabalho
    de infra (Dockerfile + Secret).

    Persistência via ``set_pipeline_dispatch_mode`` (kubectl set env + audit
    log); o pipeline pega o modo novo no rollout disparado pelo kubectl.
    """

    name = "dispatch-mode"
    title = "Modo de despacho do pipeline (deile_worker | claude)"
    refresh_s = 1.0

    HOTKEYS = ("[↑/↓] navega   [enter] aplicar   [c] limpar override   "
               "[r] refresh   [esc] volta   [q] sai")

    # Source of truth do conjunto de modos — espelha _DISPATCH_MODES_ALLOWED
    # em _panel_data.py. Listado aqui (e não importado) para a view continuar
    # importável quando o painel roda de infra/k8s sem o pacote DEILE no path.
    _MODES_FALLBACK = ("deile_worker", "claude")

    # Pretty/descrição por modo — visível no painel ajuda escolha sem doc externa.
    _MODE_DESCRIPTIONS = {
        "deile_worker": (
            "DEILE-to-DEILE — pipeline dispara o deile-worker via HTTP "
            "(default; sem dependência externa)."
        ),
        "claude": (
            "Claude Code one-shot — pipeline roda `claude -p` num worktree "
            "local. Requer binary `claude` no PATH + credenciais em "
            "~/.claude/."
        ),
    }

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.cursor: int = 0
        self.last_msg: str = ""
        self.last_ok: Optional[bool] = None
        # Modal state: None = browsing; ("set", mode) = confirming apply;
        # ("clear", None) = confirming reset.
        self.mode_modal: Optional[tuple] = None

    def on_unmount(self, app: "PanelApp") -> None:
        # Re-entry should always land on the option list, never on a stale
        # confirmation modal that the operator dismissed by leaving the view.
        self.mode_modal = None

    def intercepts_key(self, key: str) -> bool:
        # ESC inside a modal must close the modal (our own _handle_*),
        # not pop the view off the app stack.
        return key == "ESC" and self.mode_modal is not None

    # --- data accessors --------------------------------------------------

    def _current(self):
        """Return the current ``DispatchModeEntry`` (or a demo-mode stub)."""
        if self.data is None:
            # Demo mode: pretend deile-pipeline has no env override and falls
            # back to the ConfigMap default. Visible to the operator as
            # "source: default", same shape as a real cluster read.
            from _panel_data import DispatchModeEntry  # noqa: PLC0415
            return DispatchModeEntry(
                mode=None, source="default", effective="deile_worker",
            )
        return self.data.dispatch_mode.get()

    # --- rendering -------------------------------------------------------

    def _render_options(self, current_mode: Optional[str],
                        width: int) -> RenderableType:
        compact = width < 100
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        tbl.add_column(" ", width=2)
        tbl.add_column("#", width=2, justify="right")
        tbl.add_column("modo", style="bold")
        if not compact:
            tbl.add_column("descrição", overflow="fold")
        tbl.add_column("ativo", width=8, justify="center")
        modes = self._MODES_FALLBACK
        self.cursor = max(0, min(self.cursor, len(modes) - 1))
        for i, m in enumerate(modes):
            marker = "▶" if i == self.cursor else " "
            is_active = (m == current_mode)
            slug_style = "bold yellow" if is_active else "bold"
            active_mark = Text("●", style="bold green") if is_active \
                else Text("", style="dim")
            row = [
                Text(marker, style="bold cyan"),
                Text(f"{i + 1}", style="bold cyan"),
                Text(m, style=slug_style),
            ]
            if not compact:
                row.append(Text(self._MODE_DESCRIPTIONS.get(m, ""),
                                style="dim"))
            row.append(active_mark)
            tbl.add_row(*row)
        return Panel(
            tbl,
            title="[bold]MODOS DISPONÍVEIS[/bold]",
            title_align="left", border_style="cyan",
        )

    def _render_status_panel(self, entry) -> RenderableType:
        """Painel header com o estado corrente lido do cluster."""
        source_pretty = {
            "env": "DEILE_PIPELINE_DISPATCH_MODE (env do Deployment)",
            "default": "settings.json / ConfigMap (sem env override)",
        }.get(entry.source, entry.source)
        lines = [
            Text.assemble(
                ("modo efetivo: ", "dim"),
                (entry.effective, "bold yellow"),
            ),
            Text.assemble(
                ("origem: ", "dim"),
                (source_pretty, "cyan"),
            ),
        ]
        if entry.mode is None:
            lines.append(Text(
                "(sem override per-Deployment — pod usa o default do ConfigMap)",
                style="dim",
            ))
        return Panel(Group(*lines),
                     title="[bold]ESTADO ATUAL[/bold]",
                     title_align="left", border_style="yellow")

    def _render_action_status(self) -> Optional[RenderableType]:
        if not self.last_msg:
            return None
        border = "green" if self.last_ok else "red"
        return Panel(
            Text(self.last_msg,
                 style="bold green" if self.last_ok else "bold red"),
            title="[bold]ÚLTIMA AÇÃO[/bold]",
            title_align="left", border_style=border,
        )

    def _render_confirm_modal(self, kind: str) -> RenderableType:
        """Constrói o Panel de confirmação para ``set`` ou ``clear``.

        Mantém os 2 modais com a mesma moldura/posição mas headers e
        prompts distintos — único callsite no :meth:`render`.
        """
        if kind == "set":
            mode = self.mode_modal[1]
            header = Text(
                f"Aplicar dispatch_mode = '{mode}' em deile-pipeline?",
                style="bold yellow",
            )
            detail = Text(
                "`kubectl set env` dispara rollout (strategy Recreate). "
                "A próxima dispatch já roda no modo novo.",
                style="dim",
            )
            title = "[bold yellow]CONFIRMAR APLICAÇÃO[/bold yellow]"
        else:  # "clear"
            header = Text(
                "Limpar override de DEILE_PIPELINE_DISPATCH_MODE?",
                style="bold yellow",
            )
            detail = Text(
                "O pipeline voltará a usar o default declarado no "
                "ConfigMap (deile_worker) na próxima dispatch.",
                style="dim",
            )
            title = "[bold yellow]CONFIRMAR LIMPEZA[/bold yellow]"
        return Panel(
            Group(header, detail, Text(),
                  Text("[y] confirmar    [n] cancelar", style="bold cyan")),
            title=title, title_align="left", border_style="yellow",
        )

    def render(self, app: "PanelApp") -> RenderableType:
        entry = self._current()
        width = app.console.size.width or 100
        layout = Layout()
        sections: List[Layout] = [
            Layout(_head_panel(self.title, app), name="head", size=4),
        ]
        # Modal confirmação fica em destaque acima da listagem.
        modal_kind = self.mode_modal[0] if self.mode_modal is not None else None
        if modal_kind in ("set", "clear"):
            sections.append(Layout(self._render_confirm_modal(modal_kind),
                                   name="confirm", size=8))
        else:
            # Browsing — status + opções.
            sections.append(Layout(self._render_status_panel(entry),
                                   name="status_header", size=6))
            sections.append(Layout(self._render_options(entry.effective,
                                                        width),
                                   name="body"))

        action_status = self._render_action_status()
        if action_status is not None:
            sections.append(Layout(action_status, name="status", size=4))

        sections.append(Layout(_footer_panel(self.HOTKEYS),
                               name="footer", size=3))
        layout.split_column(*sections)
        return layout

    # --- input -----------------------------------------------------------

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        # Confirmação modal: y aplica, qualquer outra tecla cancela.
        modal_kind = self.mode_modal[0] if self.mode_modal is not None else None
        if modal_kind == "set":
            return self._handle_set_confirm_key(key)
        if modal_kind == "clear":
            return self._handle_clear_confirm_key(key)
        modes = self._MODES_FALLBACK
        n = len(modes)
        if key in ("UP", "k"):
            self.cursor = (self.cursor - 1) % n
            return ActionResult.refresh()
        if key in ("DOWN", "j"):
            self.cursor = (self.cursor + 1) % n
            return ActionResult.refresh()
        if key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < n:
                self.cursor = idx
                return ActionResult.refresh()
        if key in ("\r", "\n"):
            self.mode_modal = ("set", modes[self.cursor])
            return ActionResult.refresh()
        if key == "c":
            self.mode_modal = ("clear", None)
            return ActionResult.refresh()
        return ActionResult()

    def _handle_set_confirm_key(self, key: str) -> ActionResult:
        mode = self.mode_modal[1] if self.mode_modal else None
        # Guarda explícita contra mode_modal degenerado (('set', '') /
        # ('set', None)): em vez de cair silenciosamente no ramo de
        # cancelamento, fechamos com erro visível pra o operador notar.
        if key == "y" and not mode:
            self.mode_modal = None
            self.last_msg = (
                "estado interno inconsistente: modo do modal vazio"
            )
            self.last_ok = False
            pd_audit_dispatch_mode_change(
                None, result="failed",
                detail="modal state degenerado em _handle_set_confirm_key",
            )
            return ActionResult.refresh()
        if key == "y" and mode:
            self._apply_set(mode)
            self.mode_modal = None
            if self.data is not None:
                self.data.dispatch_mode._cache.invalidate()  # noqa: SLF001
            return ActionResult.refresh()
        # ESC / n / anything else cancels (default-deny). Audita
        # ``cancelled`` para paridade com ModelSwitcherView — o branch
        # ``cancelled`` em ``_audit_dispatch_mode_change`` não fica morto.
        reason = ("operador cancelou na confirmação" if key in ("n", "ESC")
                  else f"tecla inesperada na confirmação: {key!r}")
        # ``mode`` pode ser falsy (degenerate set state) — passa ``None`` ao
        # audit nesse caso pra não confundir o discriminador ``action`` (que
        # vira ``kubectl_unset_env`` quando mode é falsy). ``detail`` carrega
        # o motivo original e a string vazia/falsy é registrada lá pra log
        # analysis se necessário.
        audit_mode = mode if mode else None
        audit_detail = (
            reason if mode
            else f"{reason} (modal state degenerado: mode={mode!r})"
        )
        pd_audit_dispatch_mode_change(
            audit_mode, result="cancelled", detail=audit_detail,
        )
        self.mode_modal = None
        self.last_msg = "cancelado pelo operador"
        self.last_ok = False
        return ActionResult.refresh()

    def _handle_clear_confirm_key(self, key: str) -> ActionResult:
        if key == "y":
            self._apply_clear()
            self.mode_modal = None
            if self.data is not None:
                self.data.dispatch_mode._cache.invalidate()  # noqa: SLF001
            return ActionResult.refresh()
        # Cancelamento auditado — paridade com ModelSwitcherView e com o
        # ramo "cancelled" reconhecido em ``_audit_dispatch_mode_change``.
        reason = ("operador cancelou na confirmação" if key in ("n", "ESC")
                  else f"tecla inesperada na confirmação: {key!r}")
        pd_audit_dispatch_mode_change(
            None, result="cancelled", detail=reason,
        )
        self.mode_modal = None
        self.last_msg = "cancelado pelo operador"
        self.last_ok = False
        return ActionResult.refresh()

    # --- writes ----------------------------------------------------------

    def _apply_set(self, mode: str) -> None:
        if self.data is None:
            self.last_msg = "modo demo — nenhuma escrita aplicada"
            self.last_ok = False
            return
        ns = self.data.context.namespace if self.data.context else _NS_DEFAULT
        ok, msg = pd_set_pipeline_dispatch_mode(mode, namespace=ns)
        self.last_ok = ok
        self.last_msg = msg

    def _apply_clear(self) -> None:
        if self.data is None:
            self.last_msg = "modo demo — nenhuma escrita aplicada"
            self.last_ok = False
            return
        ns = self.data.context.namespace if self.data.context else _NS_DEFAULT
        ok, msg = pd_clear_pipeline_dispatch_mode(namespace=ns)
        self.last_ok = ok
        self.last_msg = msg


class DispatchMatrixView(View):
    """Pipeline Stage Configuration unificada (issue #309 fase 2 — Task 18).

    Substitui (Task 21 — wire de ``[d]`` no Dashboard) duas views legadas:

    - :class:`DispatchModeView` (PR #330) — global flip único
      (``DEILE_PIPELINE_DISPATCH_MODE``).
    - :class:`StageModelsView` (#305) — per-stage model override
      (``DEILE_PIPELINE_MODEL_<STAGE>``).

    O resultado é uma matriz ``N+1`` linhas × 2 colunas editáveis:

    - ``N`` linhas (uma por stage de :data:`PIPELINE_STAGES`) com colunas
      ``{Stage, Worker, Model, Source}``.
    - ``+1`` linha "Global default" para editar
      ``DEILE_PIPELINE_DISPATCH_MODE`` (worker) e
      ``DEILE_PIPELINE_MODEL`` (model) — fallback aplicado quando o stage
      não tem override próprio.

    O header da view mostra o status do Deployment ``claude-worker`` (lido
    via :meth:`StageDispatchProvider.get_claude_worker_status`): ``ready`` com
    email logado, ``NOT READY`` (deployment aplicado mas pod down), ou
    ``NÃO INSTALADO`` (com hint da action ``[I]`` para o operador instalar
    on-the-fly).

    Esta task entrega apenas o skeleton (render + navegação ↑↓ ←→ q). As
    actions ``[enter]`` (editar célula), ``[r]`` (reset célula),
    ``[L]`` (switch login) e ``[I]`` (install on-the-fly) são STUBS
    devolvendo :class:`ActionResult` neutro — Tasks 19-20 implementam os
    pickers contextuais e modais de confirmação.

    O wire de ``[d]`` no Dashboard fica para Task 21 — esta classe só é
    instanciada pelo painel após o nav dict ser atualizado lá.
    """

    name = "dispatch-matrix"
    title = "Pipeline Stage Configuration ([d])"
    refresh_s = 1.0

    HOTKEYS = (
        "[↑/↓] linha   [←/→] coluna (Worker/Model/Timeout/Retries)   "
        "[enter] editar   [r] reset   "
        "[L] switch claude login   [I] install   [U] uninstall   [q] back   "
        "[s]caling row: enter digita réplicas (0-10)   "
        "[c] cleanup on-demand (preview + confirm)   "
        "[p] editar max_parallel (parallelism do pipeline)   "
        "colunas: 0=Worker 1=Model 2=Timeout 3=Retries 4=Cost cap (USD/run)   "
        "retries=0 EXIGE confirmação: digite '0!' (fail-fast, sem retry)"
    )

    # Índice de row constante para a linha de scaling (após Global default).
    # Calculado em runtime como ``len(entries) + 1`` — mantido aqui só como
    # documentação da convenção.
    _SCALING_ROW_OFFSET = 1  # offset relativo ao len(entries): global=+0, scaling=+1

    # Source of truth do conjunto de stages — espelha
    # ``deile.orchestration.pipeline.dispatch_resolver.PIPELINE_STAGES``.
    # Listado aqui (e não importado no module scope) para a view continuar
    # importável quando o painel roda de infra/k8s sem o pacote DEILE no
    # path (mesmo padrão de :class:`StageModelsView`).
    _STAGES_FALLBACK = ("classify", "refine", "implement", "pr_review",
                        "follow_ups")

    # Fallback estático dos modelos quando ``ModelsProvider`` está vazio
    # (catálogo do YAML não carregou) ou em modo demo. Espelha o conjunto
    # bundled em ``deile/config/model_providers.yaml`` — não é doctrinaire,
    # mas garante que o picker sempre tenha opções para o operador
    # escolher (ex.: em CI sem yaml acessível ou em demo sem cluster).
    _MODELS_FALLBACK_STATIC = (
        "anthropic:claude-opus-4-7",
        "anthropic:claude-sonnet-4-6",
        "anthropic:claude-haiku-4-5",
        "openai:gpt-4",
        "openai:gpt-4-turbo",
        "deepseek:deepseek-chat",
        "google:gemini-2.5-pro",
    )

    # Sentinelas do picker — exibidas como primeira opção. Selecionar
    # qualquer uma chama ``clear_*`` (kubectl ``VAR-``) no apply.
    _CLEAR_SENTINEL_WORKER = "(global default)"
    _CLEAR_SENTINEL_MODEL = "(default — clear override)"

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        # cursor_row ∈ [0, N] — N inclusive corresponde à linha "Global
        # default" no fim da matriz.
        self.cursor_row: int = 0
        # cursor_col ∈ {0, 1, 2, 3, 4} — 0=Worker, 1=Model, 2=Timeout(s), 3=Retries, 4=Cost cap (USD/run).
        # As colunas Stage e Source não são editáveis.
        self.cursor_col: int = 0
        # --- Picker modal state (Task 19). ``None`` = browsing.
        # Forma: ``(kind, stage_or_None, options)`` onde ``kind`` é:
        #   * "worker"        — picker de worker para um stage específico
        #   * "model"         — picker de model para um stage específico
        #   * "timeout"       — input numérico de timeout_s para um stage
        #   * "retries"       — input numérico de max_retries para um stage
        #   * "global_worker" — picker de dispatch_mode (global)
        #   * "global_model"  — picker de DEILE_PREFERRED_MODEL (global)
        # ``stage_or_None`` é o nome do stage para per-stage pickers, ou
        # ``None`` para os global_*.
        # ``options`` é a lista pré-computada (filtrada) que o picker
        # exibe — captura o estado da row no momento de abertura para
        # que mudar de row durante a navegação do picker não troque a
        # lista de opções (UX previsível). Para timeout/retries o picker
        # usa ``scale_prompt`` style (entrada numérica livre em vez de lista).
        self.mode: Optional[tuple] = None
        self.picker_cursor: int = 0
        # Última ação para feedback no painel (paridade com
        # ``DispatchModeView`` / ``StageModelsView``).
        self.last_msg: str = ""
        self.last_ok: Optional[bool] = None
        # --- Background install state (issue #309 fase 2 hotfix #2) ---
        # ``bootstrap_claude_worker`` chama ``claude auth login`` que pode
        # bloquear até 5 min em ``subprocess.run``. O painel TUI tem event
        # loop síncrono — chamar isso inline congela ESC, refresh e qualquer
        # tecla. Solução: rodar em ``threading.Thread`` daemon, com flag
        # para o handler de tecla rejeitar [I]/[L] concorrentes, e a thread
        # publicar resultado em ``last_msg``/``last_ok`` quando termina.
        self._install_thread: Optional["threading.Thread"] = None
        self._install_in_progress: bool = False
        # Cleanup on-demand state (issue #408):
        # mode == ("cleanup_confirm", preview_text, []) → aguarda Y/N.
        # mode == ("max_parallel_prompt", None, [buf]) → input numérico/auto.

    # --- View lifecycle / key interception ------------------------------

    def on_unmount(self, app) -> None:
        # Reentry sempre na matriz, nunca num picker stale.
        self.mode = None
        self.picker_cursor = 0

    def intercepts_key(self, key: str) -> bool:
        # ESC dentro do picker fecha o modal (handle_key), não pop a view.
        return key == "ESC" and self.mode is not None

    # --- data accessors --------------------------------------------------

    def _stages(self) -> tuple:
        """Lazy import de :data:`PIPELINE_STAGES` com fallback estático.

        Catch EXCEPTION (não só ImportError): se o pacote ``deile`` tem
        SyntaxError numa cadeia de import (ex.: merge conflict não
        resolvido em ``deile/tools/discovery.py``), o ``import`` levanta
        ``SyntaxError`` (não ``ImportError``). O painel NÃO PODE crashar
        por causa de código quebrado em outro módulo — usa o fallback
        estático que já é a fonte autoritativa do PIPELINE_STAGES (Task 17
        ``StageDispatchProvider`` faz a mesma proteção).
        """
        try:
            from deile.orchestration.pipeline.dispatch_resolver import \
                PIPELINE_STAGES  # noqa: PLC0415
            return PIPELINE_STAGES
        except Exception:  # noqa: BLE001 — proteção genérica é intencional
            return self._STAGES_FALLBACK

    def _entries(self) -> List:
        """Retorna as 5 entries do :class:`StageDispatchProvider`.

        Em modo demo (``data=None``), monta entries vazias para a UI ainda
        renderizar — o operador vê a estrutura da matriz mesmo sem cluster.
        """
        if self.data is None:
            from _panel_data import StageDispatchEntry  # noqa: PLC0415
            return [
                StageDispatchEntry(s, "deile-worker", None, "default",
                                   cost_cap_usd=None)
                for s in self._stages()
            ]
        return self.data.stage_dispatch.get_all_stages()

    def _load_all_models(self) -> List[str]:
        """Lista de slugs do catálogo (``ModelsProvider``) com fallback.

        Quando ``data`` é ``None`` ou o provider está vazio (yaml não
        carregou), cai no ``_MODELS_FALLBACK_STATIC`` para o picker ainda
        ter opções.
        """
        if self.data is not None:
            try:
                models = self.data.models.get()
            except Exception:  # noqa: BLE001
                # MagicMock pode não ter ``.models`` configurado; cai no
                # fallback para o teste/demo ainda funcionar.
                models = []
            if models:
                slugs = []
                for m in models:
                    slug = getattr(m, "slug", None)
                    if isinstance(slug, str) and slug:
                        slugs.append(slug)
                if slugs:
                    return slugs
        return list(self._MODELS_FALLBACK_STATIC)

    # --- picker option builders (Task 19) -------------------------------

    def _worker_picker_options(self) -> List[str]:
        """Opções do picker de worker (3 itens fixos)."""
        return [
            self._CLEAR_SENTINEL_WORKER,  # primeiro = "limpar override"
            "deile-worker",
            "claude-worker",
        ]

    def _model_picker_options(self, *, worker: str) -> List[str]:
        """Opções do picker de model, contextualizadas por ``worker``.

        ``worker == "claude-worker"`` restringe o catálogo a ``anthropic:*``
        (único provider que o claude binary aceita). Qualquer outro worker
        (``deile-worker`` é o padrão) mostra o catálogo completo.
        """
        all_models = self._load_all_models()
        if worker == "claude-worker":
            filtered = [m for m in all_models if m.startswith("anthropic:")]
            return [self._CLEAR_SENTINEL_MODEL, *filtered]
        return [self._CLEAR_SENTINEL_MODEL, *all_models]

    def _claude_status(self):
        """Status do Deployment ``claude-worker``. Demo → não instalado."""
        if self.data is None:
            from _panel_data import ClaudeWorkerStatus  # noqa: PLC0415
            return ClaudeWorkerStatus(
                deployment_applied=False, pod_ready=False,
                logged_in_email=None,
            )
        return self.data.stage_dispatch.get_claude_worker_status()

    # --- rendering -------------------------------------------------------

    def render(self, app) -> RenderableType:
        """Wrapper defensivo: catch ANY exception → renderiza painel de
        erro com stacktrace truncado em vez de crashar o loop principal.

        Princípio enterprise: a view do painel TUI nunca pode derrubar o
        processo. Erros em providers (kubectl down, syntax error em cadeia
        de import, secret malformado etc) viram feedback visual com hint
        de ação.
        """
        try:
            return self._render_safe(app)
        except Exception as exc:  # noqa: BLE001 — proteção genérica do view
            import traceback as _tb  # noqa: PLC0415
            tb_lines = _tb.format_exception_only(type(exc), exc)
            tb_text = "".join(tb_lines).strip()
            # Mantém o que conseguimos do render normal — pelo menos o
            # rodapé de hotkeys e o painel de erro.
            return Group(
                Text("DispatchMatrixView: render falhou",
                     style="bold red"),
                Panel(
                    Text(tb_text, style="red"),
                    title="[bold red]ERRO DO PAINEL[/bold red]",
                    title_align="left", border_style="red",
                ),
                Text(self.HOTKEYS, style="dim"),
            )

    def _render_safe(self, app) -> RenderableType:
        """Render real (separado de :meth:`render` que é o wrapper de erro)."""
        entries = self._entries()
        cw = self._claude_status()

        # --- Header: claude-worker status -------------------------------
        if cw.deployment_applied:
            ready_label = "ready" if cw.pod_ready else "NOT READY"
            email_part = (f"  (logado como: {cw.logged_in_email})"
                          if cw.logged_in_email else "")
            status_text = f"claude-worker: {ready_label}{email_part}"
            status_style = "bold green" if cw.pod_ready else "bold yellow"
        else:
            # Hint claro da action [I] que instala o Deployment on-the-fly
            # (Task 20). Operador vê o caminho a seguir sem ler doc externa.
            status_text = (
                "claude-worker: NÃO INSTALADO  "
                "([I] para instalar; [d] depois pra configurar stages)"
            )
            status_style = "dim yellow"

        # --- Matrix table -----------------------------------------------
        # Sem ``width=N`` literal em add_column — princípio 15
        # (UI resize-adaptativa, issue #307). Rich auto-calcula a largura
        # ótima por coluna em cada render usando ``console.width`` corrente.
        tbl = Table(show_header=True, box=box.SIMPLE_HEAVY, expand=True)
        tbl.add_column("Stage", style="bold cyan")
        tbl.add_column("Worker")
        tbl.add_column("Model")
        tbl.add_column("Timeout (s)")
        tbl.add_column("Max retries")
        tbl.add_column("Cost cap (USD/run)")
        tbl.add_column("Source", style="dim")

        for i, entry in enumerate(entries):
            highlight_w = (i == self.cursor_row and self.cursor_col == 0)
            highlight_m = (i == self.cursor_row and self.cursor_col == 1)
            highlight_t = (i == self.cursor_row and self.cursor_col == 2)
            highlight_r = (i == self.cursor_row and self.cursor_col == 3)
            highlight_c = (i == self.cursor_row and self.cursor_col == 4)

            worker_cell = (f"[reverse]{entry.worker}[/reverse]"
                           if highlight_w else entry.worker)
            model_txt = entry.model or "(default)"
            model_cell = (f"[reverse]{model_txt}[/reverse]"
                          if highlight_m else model_txt)
            timeout_txt = str(entry.timeout_s) if entry.timeout_s is not None else "(default)"
            timeout_cell = (f"[reverse]{timeout_txt}[/reverse]"
                            if highlight_t else timeout_txt)
            retries_txt = str(entry.max_retries) if entry.max_retries is not None else "(default)"
            retries_cell = (f"[reverse]{retries_txt}[/reverse]"
                            if highlight_r else retries_txt)
            cap_raw = getattr(entry, "cost_cap_usd", None)
            cap_txt = (f"${cap_raw}" if cap_raw else "(no cap)")
            cost_cap_cell = (f"[reverse]{cap_txt}[/reverse]"
                             if highlight_c else cap_txt)
            tbl.add_row(entry.stage, worker_cell, model_cell,
                        timeout_cell, retries_cell, cost_cap_cell, entry.source)

        # Separador visual entre stages e a linha "Global default".
        tbl.add_row("─" * 12, "─" * 14, "─" * 28, "─" * 10, "─" * 11, "─" * 12, "─" * 8,
                    style="dim")

        # Linha "Global default" — ``cursor_row == len(entries)`` aponta
        # aqui (mas só dentro dos limites de ``handle_key``).
        global_idx = len(entries)
        highlight_gw = (self.cursor_row == global_idx
                        and self.cursor_col == 0)
        highlight_gm = (self.cursor_row == global_idx
                        and self.cursor_col == 1)
        highlight_gt = (self.cursor_row == global_idx
                        and self.cursor_col == 2)
        highlight_gr = (self.cursor_row == global_idx
                        and self.cursor_col == 3)
        global_w_txt = "(DEILE_PIPELINE_DISPATCH_MODE)"
        global_m_txt = "(DEILE_PIPELINE_MODEL)"
        global_t_txt = "(DEILE_PIPELINE_DEILE/CLAUDE_TIMEOUT)"
        global_r_txt = "(DEILE_PIPELINE_DEFAULT_MAX_RETRIES)"
        if highlight_gw:
            global_w_txt = f"[reverse]{global_w_txt}[/reverse]"
        if highlight_gm:
            global_m_txt = f"[reverse]{global_m_txt}[/reverse]"
        if highlight_gt:
            global_t_txt = f"[reverse]{global_t_txt}[/reverse]"
        if highlight_gr:
            global_r_txt = f"[reverse]{global_r_txt}[/reverse]"
        tbl.add_row("Global default", global_w_txt, global_m_txt,
                    global_t_txt, global_r_txt, "—", "env")

        # Linha "Worker Scaling" — edita réplicas de deile-worker / claude-worker
        # via prompt numérico [enter] (issue #309 fase 3 Task 4).
        # ``cursor_row == len(entries) + 1`` aponta aqui.
        scaling_idx = len(entries) + 1
        highlight_sw = (self.cursor_row == scaling_idx and self.cursor_col == 0)
        highlight_sm = (self.cursor_row == scaling_idx and self.cursor_col == 1)
        scale_w_txt = "(réplicas deile-worker)"
        scale_m_txt = "(réplicas claude-worker)"
        if highlight_sw:
            scale_w_txt = f"[reverse]{scale_w_txt}[/reverse]"
        if highlight_sm:
            scale_m_txt = f"[reverse]{scale_m_txt}[/reverse]"
        tbl.add_row("Worker Scaling", scale_w_txt, scale_m_txt,
                    "", "", "—", "kubectl")

        # Linha "Max Parallel" — edita DEILE_PIPELINE_MAX_PARALLEL no
        # Deployment deile-pipeline (issue #408). [p] ou [enter] nesta row
        # abre prompt numérico/auto.
        # ``cursor_row == len(entries) + 2`` aponta aqui.
        max_parallel_idx = len(entries) + 2
        highlight_mp = (self.cursor_row == max_parallel_idx
                        and self.cursor_col == 0)
        mp_current = self._read_max_parallel_env()
        mp_txt = mp_current if mp_current else "(default: 2)"
        mp_desc = "auto = réplicas claude-worker" if mp_current == "auto" else "DEILE_PIPELINE_MAX_PARALLEL"
        if highlight_mp:
            mp_txt = f"[reverse]{mp_txt}[/reverse]"
        tbl.add_row("Max Parallel", mp_txt, mp_desc, "", "", "—", "env")

        # --- Compose: header + (banner?) + matrix + (picker?) + (status?) + hotkeys --
        parts: List[RenderableType] = [
            Text(status_text, style=status_style),
        ]
        banner = self._render_overrides_banner(entries)
        if banner is not None:
            parts.append(banner)
        parts.append(tbl)
        if self.mode is not None:
            parts.append(self._render_picker())
        if self.last_msg:
            border = "green" if self.last_ok else "red"
            parts.append(Panel(
                Text(self.last_msg,
                     style="bold green" if self.last_ok else "bold red"),
                title="[bold]ÚLTIMA AÇÃO[/bold]",
                title_align="left", border_style=border,
            ))
        parts.append(Text(self.HOTKEYS, style="dim"))
        return Group(*parts)

    def _render_overrides_banner(self, entries: List) -> Optional[RenderableType]:
        """Banner C — lista overrides ativos por stage com destaque.

        Lê a mesma snapshot já carregada em ``entries`` (sem novo round-trip
        kubectl). Mostra um Panel acima da matriz quando existir pelo menos
        um override per-stage (worker, model, timeout_s, max_retries,
        cost_cap_usd). ``retries=0`` recebe destaque vermelho — é o caso
        que historicamente bloqueou o pipeline silenciosamente
        (issue ghost env ``DEILE_PIPELINE_RETRIES_IMPLEMENT=0``).

        Retorna ``None`` quando não há overrides — mantém UX limpa.
        """
        lines: List[Text] = []
        for entry in entries:
            stage = getattr(entry, "stage", None)
            if not stage:
                continue
            bits: List[Text] = []
            # worker override é só relevante quando source == "env"
            # (per-stage); "global"/"default" não conta como override
            # do stage.
            source = getattr(entry, "source", None)
            worker = getattr(entry, "worker", None)
            if source == "env" and worker:
                bits.append(Text(f"worker={worker}", style="yellow"))
            model = getattr(entry, "model", None)
            if model:
                bits.append(Text(f"model={model}", style="cyan"))
            timeout_s = getattr(entry, "timeout_s", None)
            if timeout_s is not None:
                bits.append(Text(f"timeout={timeout_s}s", style="yellow"))
            max_retries = getattr(entry, "max_retries", None)
            if max_retries is not None:
                style = "bold red reverse" if max_retries == 0 else "yellow"
                label = (f"retries=0 ⚠ FAIL-FAST" if max_retries == 0
                         else f"retries={max_retries}")
                bits.append(Text(label, style=style))
            cap = getattr(entry, "cost_cap_usd", None)
            if cap:
                bits.append(Text(f"cap=${cap}", style="yellow"))
            if not bits:
                continue
            line = Text(f"  {stage}: ", style="bold")
            for i, bit in enumerate(bits):
                if i:
                    line.append("  ")
                line.append_text(bit)
            lines.append(line)
        if not lines:
            return None
        return Panel(
            Group(*lines),
            title="[bold]OVERRIDES ATIVOS (per-stage)[/bold]",
            title_align="left",
            border_style="yellow",
            padding=(0, 1),
        )

    def _render_picker(self) -> RenderableType:
        """Renderiza o Panel do picker corrente (worker / model / global /
        install-confirm / switch-login-confirm).
        """
        kind, stage, options = self.mode  # type: ignore[misc]
        # Título descritivo para o operador entender o que vai mudar.
        if kind == "worker":
            title = f"ESCOLHA WORKER PARA STAGE '{stage}'"
        elif kind == "model":
            title = f"ESCOLHA MODEL PARA STAGE '{stage}'"
        elif kind == "timeout":
            title = f"TIMEOUT (s) PARA STAGE '{stage}' (enter vazio = clear)"
        elif kind == "retries":
            title = f"MAX RETRIES PARA STAGE '{stage}' (enter vazio = clear)"
        elif kind == "cost_cap_usd":
            title = f"COST CAP (USD/RUN) PARA STAGE '{stage}'"
        elif kind == "global_worker":
            title = "ESCOLHA DISPATCH MODE GLOBAL (DEILE_PIPELINE_DISPATCH_MODE)"
        elif kind == "global_model":
            title = "ESCOLHA MODEL GLOBAL (DEILE_PREFERRED_MODEL)"
        elif kind == "install_confirm":
            # ``stage`` carrega o prompt textual para a confirmação
            # (reuso do slot, sem alocar campo dedicado no tuple).
            title = "INSTALAR CLAUDE-WORKER?"
        elif kind == "switch_login_confirm":
            title = "TROCAR CONTA DO CLAUDE-WORKER?"
        elif kind == "uninstall_confirm":
            title = "DESINSTALAR CLAUDE-WORKER?"
        elif kind == "scale_prompt":
            title = f"RÉPLICAS PARA '{stage}' (0-10)"
        elif kind == "cleanup_confirm":
            title = "CLEANUP ON-DEMAND — CONFIRMAR?"
        elif kind == "max_parallel_prompt":
            title = "MAX_PARALLEL DO PIPELINE (DEILE_PIPELINE_MAX_PARALLEL)"
        else:  # defensivo
            title = "AÇÃO"

        if not options:
            return Panel(
                Text("(sem opções disponíveis)", style="dim"),
                title=f"[bold yellow]{title}[/bold yellow]",
                title_align="left", border_style="yellow",
            )

        self.picker_cursor = max(0, min(self.picker_cursor, len(options) - 1))
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False,
                    show_header=False)
        # max_width=2 (teto, princípio 15) — Rich encolhe se o terminal for
        # estreito; o marcador ▶/espaço ocupa sempre 1 char, nunca mais.
        tbl.add_column(" ", max_width=2)
        tbl.add_column("opção", style="bold")
        for i, opt in enumerate(options):
            marker = "▶" if i == self.picker_cursor else " "
            tbl.add_row(Text(marker, style="bold cyan"), Text(opt))

        # Modais de confirmação Y/N usam um prompt e atalhos Y/N visíveis;
        # pickers de entrada numérica livre (timeout/retries/max_parallel) mostram buffer;
        # pickers convencionais usam enter/esc.
        if kind in ("install_confirm", "switch_login_confirm",
                    "uninstall_confirm"):
            # ``stage`` carrega o texto explicativo da operação.
            prompt = Text(str(stage) if stage else "", style="bold")
            hint = Text("[Y] confirma   [N]/[esc] cancela",
                        style="dim cyan")
            body: RenderableType = Group(prompt, Text(""), tbl, hint)
        elif kind == "cleanup_confirm":
            # ``stage`` carrega o preview do que vai ser apagado.
            prompt = Text(str(stage) if stage else "(sem itens a remover)", style="bold")
            hint = Text("[Y] confirma cleanup   [N]/[esc] cancela",
                        style="dim cyan")
            body = Group(prompt, Text(""), hint)
        elif kind in ("timeout", "retries"):
            # Entrada numérica livre — ``options[0]`` é o buffer atual.
            buf = options[0] if options else ""
            input_line = Text(f"› {buf}_", style="bold cyan")
            if kind == "timeout":
                unit_hint = " (segundos, > 0)"
            else:  # retries
                unit_hint = " (>= 0)"
            hint_line = Text(
                f"[0-9] digita   [backspace] apaga   [enter] confirma   [esc] cancela{unit_hint}",
                style="dim cyan",
            )
            body = Group(input_line, Text(""), hint_line)
        elif kind == "max_parallel_prompt":
            # ``options[0]`` é o buffer atual (número ou "auto").
            buf = options[0] if options else ""
            input_line = Text(f"› {buf}_", style="bold cyan")
            hint_line = Text(
                "[0-9] digita   [a] = 'auto' (replica count)   "
                "[backspace] apaga   [enter] confirma   [esc] cancela",
                style="dim cyan",
            )
            body = Group(input_line, Text(""), hint_line)
        else:
            body = Group(
                tbl,
                Text("[↑/↓] navega   [enter] confirma   [esc] cancela",
                     style="dim cyan"),
            )

        return Panel(
            body,
            title=f"[bold yellow]{title}[/bold yellow]",
            title_align="left", border_style="yellow",
        )

    # --- input -----------------------------------------------------------

    def handle_key(self, key: str, app) -> ActionResult:
        """Wrapper defensivo: catch ANY exception → mostra erro em
        ``last_msg`` (vermelho) em vez de propagar pro main loop (que
        derrubaria o painel inteiro).

        Princípio enterprise: input handling do painel TUI NUNCA pode
        crashar — operador precisa do painel up mesmo quando uma action
        falhou.
        """
        try:
            return self._handle_key_safe(key, app)
        except Exception as exc:  # noqa: BLE001 — proteção genérica intencional
            self.last_msg = (
                f"erro no handler de tecla {key!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            self.last_ok = False
            # Fecha modal se estava aberto pra não travar
            self.mode = None
            self.picker_cursor = 0
            return ActionResult.refresh()

    def _handle_key_safe(self, key: str, app) -> ActionResult:
        """Roteador de teclas real (separado de :meth:`handle_key` que é
        wrapper defensivo).

        - Picker modal ativo → :meth:`_handle_picker_key` (↑/↓ navega,
          enter confirma, ESC cancela).
        - Browsing (sem modal):
            * Navegação ↑/↓ row, ←/→ col com clamp em [0, N] × [0, 1].
            * [q]/ESC → :meth:`ActionResult.nav` para o dashboard.
            * [enter] → abre picker contextual:
                - row < N + col 0 → :meth:`_open_worker_picker`
                - row < N + col 1 → :meth:`_open_model_picker`
                - row == N (Global default) → picker global
                  (worker/model conforme col).
            * [r] → reseta a célula corrente (clear override).
            * [L] → switch claude-worker login.
            * [I] → install claude-worker on-the-fly.
        """
        # Picker modal — todas as teclas vão pro handler dedicado.
        if self.mode is not None:
            return self._handle_picker_key(key)

        # Última linha editável é "Max Parallel" (len+2; Global=len, Scaling=len+1).
        # Issue #408 adicionou linha "Max Parallel" após "Worker Scaling".
        max_row = len(self._stages()) + 2  # +2 = Worker Scaling + Max Parallel

        # --- back / quit --------------------------------------------------
        if key == "q" or key == "ESC":
            # Tasks futuras podem trocar para ``ActionResult.back()`` se o
            # painel mantiver stack — por ora, nav explícita ao dashboard.
            return ActionResult.nav("dashboard")

        # --- [c] cleanup on-demand ----------------------------------------
        if key == "c":
            return self._open_cleanup_confirm()

        # --- [p] editar max_parallel ---------------------------------------
        if key == "p":
            return self._open_max_parallel_prompt()

        # --- navegação row ------------------------------------------------
        if key in ("UP", "k"):
            self.cursor_row = max(0, self.cursor_row - 1)
            return ActionResult.refresh()
        if key in ("DOWN", "j"):
            self.cursor_row = min(max_row, self.cursor_row + 1)
            return ActionResult.refresh()

        # --- navegação col ------------------------------------------------
        if key in ("LEFT", "h"):
            self.cursor_col = max(0, self.cursor_col - 1)
            return ActionResult.refresh()
        if key in ("RIGHT", "l"):
            # 5 editable columns: 0=Worker, 1=Model, 2=Timeout, 3=Retries, 4=Cost cap (issues #391/#392)
            self.cursor_col = min(4, self.cursor_col + 1)
            return ActionResult.refresh()

        # --- [enter]: abre picker contextual ------------------------------
        if key in ("\r", "\n"):
            entries = self._entries()
            n_stages = len(entries)
            global_idx = n_stages           # "Global default"
            scaling_idx = n_stages + 1      # "Worker Scaling"
            max_parallel_idx = n_stages + 2  # "Max Parallel" (issue #408)
            if self.cursor_row == max_parallel_idx:
                # Linha "Max Parallel" → prompt numérico/auto (issue #408).
                return self._open_max_parallel_prompt()
            if self.cursor_row == scaling_idx:
                # Linha de scaling → prompt numérico de réplicas (Task 4).
                # Cols 2/3 (timeout/retries) não se aplicam à scaling row.
                if self.cursor_col >= 2:
                    self.last_msg = "timeout/retries não se aplicam à linha de scaling"
                    self.last_ok = None
                    return ActionResult.refresh()
                return self._open_scaling_prompt()
            # Row na linha "Global default" → picker global (só worker/model).
            if self.cursor_row == global_idx:
                if self.cursor_col == 0:
                    return self._open_global_worker_picker()
                if self.cursor_col == 1:
                    return self._open_global_model_picker()
                # cols 2/3 (Timeout/Retries) no Global row são read-only hint
                self.last_msg = (
                    "Timeout/Retries globais: editar via settings.json ou "
                    "env vars DEILE_PIPELINE_TIMEOUT_S_*/DEILE_PIPELINE_RETRIES_*"
                )
                self.last_ok = None
                return ActionResult.refresh()
            # Row dentro das stages → picker per-stage.
            entry = entries[self.cursor_row]
            if self.cursor_col == 0:
                return self._open_worker_picker(entry)
            elif self.cursor_col == 1:
                return self._open_model_picker(entry)
            elif self.cursor_col == 2:
                return self._open_timeout_prompt(entry)
            elif self.cursor_col == 3:
                return self._open_retries_prompt(entry)
            else:
                # col 4 = Cost cap (issue #392)
                return self._open_cost_cap_picker(entry)

        # --- [r] reset da célula corrente -------------------------------
        if key == "r":
            return self._reset_current_cell()
        if key in ("L", "I"):
            # Bloqueia spawn duplo enquanto bootstrap em background.
            if self._install_in_progress:
                self.last_msg = (
                    "instalação/login do claude-worker em andamento — "
                    "aguarde o resultado aparecer aqui"
                )
                self.last_ok = None
                return ActionResult.refresh()
        if key in ("L",):
            # Task 20 — modal de switch-login do claude-worker. Só faz
            # sentido quando o Deployment já está aplicado; senão sugere [I].
            cw_status = self._claude_status()
            if not cw_status.deployment_applied:
                self.last_msg = (
                    "claude-worker não está instalado — use [I] para "
                    "instalar primeiro"
                )
                self.last_ok = False
                return ActionResult.refresh()
            return self._open_switch_login_modal(
                current_email=cw_status.logged_in_email,
            )
        if key in ("I",):
            # Task 20 — install on-the-fly. Só abre o modal se o Deployment
            # ainda NÃO foi aplicado; caso contrário avisa e aponta [L].
            cw_status = self._claude_status()
            if cw_status.deployment_applied:
                self.last_msg = (
                    "claude-worker já está instalado — use [L] para "
                    "trocar de conta ou [U] para desinstalar"
                )
                self.last_ok = None  # informativo, sem vermelho
                return ActionResult.refresh()
            return self._open_install_modal()
        if key in ("U",):
            # Hotfix #309 fase 2 — uninstall on-the-fly. Útil quando rollout
            # falha na metade ("did not become ready in time") e operador
            # quer reinstalar do zero. Idempotente; recursos ausentes não
            # crasham. Aceita SEMPRE (mesmo sem deployment_applied) porque
            # install parcial pode deixar Secret/PVC órfãos que precisam
            # limpeza.
            if self._install_in_progress:
                self.last_msg = (
                    "instalação/login do claude-worker em andamento — "
                    "aguarde o resultado antes de desinstalar"
                )
                self.last_ok = None
                return ActionResult.refresh()
            return self._open_uninstall_modal()

        return ActionResult()

    # --- picker openers --------------------------------------------------

    def _open_worker_picker(self, entry) -> ActionResult:
        """Abre picker per-stage de worker (col 0 numa stage row)."""
        self.mode = ("worker", entry.stage, self._worker_picker_options())
        self.picker_cursor = 0
        return ActionResult.refresh()

    def _open_model_picker(self, entry) -> ActionResult:
        """Abre picker per-stage de model (col 1 numa stage row).

        O picker é contextualizado pelo ``worker`` corrente da MESMA linha
        (entry.worker) — escolher um worker ``claude-worker`` restringe
        a lista de models a ``anthropic:*``.
        """
        opts = self._model_picker_options(worker=entry.worker)
        self.mode = ("model", entry.stage, opts)
        self.picker_cursor = 0
        return ActionResult.refresh()

    def _open_global_worker_picker(self) -> ActionResult:
        """Abre picker global de worker (DEILE_PIPELINE_DISPATCH_MODE)."""
        # Global picker NÃO oferece "(global default)" (que é ele mesmo).
        # Em vez disso, oferece "(clear override)" + os 2 valores válidos.
        opts = ["(clear override)", "deile-worker", "claude-worker"]
        self.mode = ("global_worker", None, opts)
        self.picker_cursor = 0
        return ActionResult.refresh()

    def _open_global_model_picker(self) -> ActionResult:
        """Abre picker global de model (DEILE_PREFERRED_MODEL em deile-worker)."""
        # Global model picker mostra TODOS os providers — sem
        # restrição por worker (worker é per-stage).
        all_models = self._load_all_models()
        opts = ["(clear override)", *all_models]
        self.mode = ("global_model", None, opts)
        self.picker_cursor = 0
        return ActionResult.refresh()

    def _open_timeout_prompt(self, entry) -> ActionResult:
        """Abre prompt numérico de timeout_s para o stage da entry.

        Reutiliza o ``scale_prompt`` style (entrada numérica livre) — a
        :meth:`_render_picker` renderiza com prompt de texto e
        :meth:`_handle_picker_key` captura dígitos + [backspace] + [enter].
        ``options`` carrega o valor atual para pré-preencher o prompt.
        """
        current = str(entry.timeout_s) if entry.timeout_s is not None else ""
        self.mode = ("timeout", entry.stage, [current])
        self.picker_cursor = 0
        return ActionResult.refresh()

    def _open_retries_prompt(self, entry) -> ActionResult:
        """Abre prompt numérico de max_retries para o stage da entry."""
        current = str(entry.max_retries) if entry.max_retries is not None else ""
        self.mode = ("retries", entry.stage, [current])
        self.picker_cursor = 0
        return ActionResult.refresh()

    def _open_cost_cap_picker(self, entry) -> ActionResult:
        """Abre picker de cost cap (USD/run) para um stage (col 4, issue #392).

        Oferece presets comuns + "(no cap)" para limpar o override.
        O operador pode também digitar um valor decimal positivo via entrada
        livre (``cost_cap_usd`` kind aceita qualquer decimal positivo).
        """
        current = getattr(entry, "cost_cap_usd", None)
        # Common preset values + current (if set and not a preset) + "(no cap)".
        presets = ["(no cap)", "1.00", "2.00", "5.00", "10.00", "20.00", "50.00"]
        if current and current not in presets:
            opts = ["(no cap)", current, *presets[1:]]
        else:
            opts = presets
        self.mode = ("cost_cap_usd", entry.stage, opts)
        self.picker_cursor = 0
        return ActionResult.refresh()

    # --- [r] reset cell --------------------------------------------------

    def _reset_current_cell(self) -> ActionResult:
        """Clear do override da célula corrente — volta ao fallback chain.

        Roteia conforme (cursor_row, cursor_col):
          - Stage row + col 0 (Worker)  → set_pipeline_dispatch_stage(stage, None)
          - Stage row + col 1 (Model)   → clear_stage_model(stage)
          - Stage row + col 2 (Timeout) → reset_stage_timeout_s(stage)
          - Stage row + col 3 (Retries) → reset_stage_retries(stage)
          - Global row + col 0 (Worker) → clear_pipeline_dispatch_mode()
          - Global row + col 1+ → no-op com hint

        Cache do StageDispatchProvider é invalidado depois — próximo render
        mostra o valor novo.
        """
        if self.data is None:
            self.last_msg = "[demo] reset (sem cluster, no-op)"
            self.last_ok = False
            return ActionResult.refresh()

        entries = self._entries()
        n_stages = len(entries)
        ns = (self.data.context.namespace
              if getattr(self.data, "context", None)
              else getattr(self.data, "namespace", _NS_DEFAULT))

        if self.cursor_row < n_stages:
            entry = entries[self.cursor_row]
            stage = entry.stage
            if self.cursor_col == 0:
                ok, msg = pd_set_pipeline_dispatch_stage(stage, None, namespace=ns)
            elif self.cursor_col == 1:
                ok, msg = pd_clear_stage_model(stage)
            elif self.cursor_col == 2:
                ok, msg = pd_set_stage_timeout(stage, None, namespace=ns)
            elif self.cursor_col == 3:
                ok, msg = pd_set_stage_retries(stage, None, namespace=ns)
            else:
                # col 4 = Cost cap reset (issue #392)
                ok, msg = pd_reset_stage_cost_cap_usd(stage, namespace=ns)
        elif self.cursor_row == n_stages + 2:  # Pipeline Parallelism row (issue #408)
            ok, msg = pd_set_pipeline_max_parallel(None, namespace=ns)
        elif self.cursor_row == n_stages + 1:  # Worker Scaling row — no reset
            ok, msg = (None, "scaling row não tem reset — ajuste manualmente")
        else:  # Global default row (n_stages)
            if self.cursor_col == 0:
                ok, msg = pd_clear_pipeline_dispatch_mode(namespace=ns)
            else:
                ok, msg = (None, "clear de override global ainda não suportado via painel")

        self.last_ok = ok
        self.last_msg = msg
        # Invalida cache pra próximo render refletir o reset.
        try:
            self.data.stage_dispatch._cache.invalidate()  # noqa: SLF001
            self.data.stage_dispatch._status_cache.invalidate()  # noqa: SLF001
        except (AttributeError, TypeError):
            pass
        return ActionResult.refresh()

    # --- install / switch-login modals (Task 20) ------------------------

    def _open_install_modal(self) -> ActionResult:
        """Modal Y/N para instalar o claude-worker on-the-fly.

        Reusa o mesmo slot ``self.mode`` dos pickers; o ``stage`` slot
        carrega o prompt textual (renderizado por :meth:`_render_picker`
        com layout especializado). Confirmação por Y; cancelamento por N
        ou ESC.
        """
        prompt = (
            "Vou: (1) capturar credenciais (claude login se necessário); "
            "(2) criar Secret claude-credentials; (3) aplicar manifests; "
            "(4) aguardar Ready do pod."
        )
        self.mode = ("install_confirm", prompt,
                     ["Sim, instalar agora", "Cancelar"])
        self.picker_cursor = 0
        self.last_msg = ""
        self.last_ok = None
        return ActionResult.refresh()

    def _open_switch_login_modal(
        self, *, current_email: Optional[str],
    ) -> ActionResult:
        """Modal Y/N para trocar a conta logada no claude-worker.

        Exibe o email corrente (best-effort — lido do Secret
        ``claude-credentials`` pelo provider). Confirmação dispara
        ``bootstrap_claude_worker(force_relogin=True)`` que faz
        ``claude logout`` + nova OAuth no host antes de re-aplicar o Secret.
        """
        email_repr = current_email or "(desconhecido)"
        prompt = (
            f"Conta atual: {email_repr}. Browser vai abrir para nova "
            "OAuth; depois o Secret é re-aplicado e o Deployment "
            "reiniciado."
        )
        self.mode = ("switch_login_confirm", prompt,
                     ["Sim, trocar conta", "Cancelar"])
        self.picker_cursor = 0
        self.last_msg = ""
        self.last_ok = None
        return ActionResult.refresh()

    def _open_uninstall_modal(self) -> ActionResult:
        """Modal Y/N para desinstalar o claude-worker do cluster.

        Spawnado por ``[U]``. Idempotente — funciona mesmo se install
        parcial deixou Secret/PVC órfãos. Deleta: Deployment, Service,
        PVC ``claude-worker-home``, Secrets ``claude-credentials`` +
        ``claude-worker-bearer``, ConfigMap allowed-repos. NetworkPolicy
        NÃO é tocada (compartilhada com deile-worker).
        """
        prompt = (
            "Vou deletar do cluster: Deployment + Service "
            "+ PVC (claude-worker-home) + Secrets (claude-credentials, "
            "claude-worker-bearer) + ConfigMap (allowed-repos). "
            "Operação idempotente — recursos ausentes são ignorados. "
            "NetworkPolicy NÃO é alterada (compartilhada com deile-worker)."
        )
        self.mode = ("uninstall_confirm", prompt,
                     ["Sim, desinstalar agora", "Cancelar"])
        self.picker_cursor = 0
        self.last_msg = ""
        self.last_ok = None
        return ActionResult.refresh()

    # --- Worker Scaling prompt (issue #309 fase 3 Task 4) ---------------

    def _open_scaling_prompt(self) -> ActionResult:
        """Abre o modal de edição numérica de réplicas.

        Reusa o slot ``self.mode`` com kind ``"scale_prompt"``. O campo
        ``stage`` carrega qual coluna está sendo editada:
        ``"deile-worker"`` (col 0) ou ``"claude-worker"`` (col 1).
        ``options`` carrega as opções numéricas [0..10] como strings.
        """
        deploy_name = ("deile-worker" if self.cursor_col == 0
                       else "claude-worker")
        opts = [str(n) for n in range(11)]  # 0 a 10
        self.mode = ("scale_prompt", deploy_name, opts)
        self.picker_cursor = 1  # default: 1 réplica
        self.last_msg = ""
        self.last_ok = None
        return ActionResult.refresh()

    def _apply_scaling(self, deploy_name: str, replicas: int) -> ActionResult:
        """Executa ``kubectl scale deployment/<name> --replicas=N``.

        Roda inline (síncrono) — operação rápida (~100ms kubectl API call).
        Para install/uninstall (que podem bloquear minutos), usamos thread;
        scale é fire-and-forget com retorno imediato do API server.
        """
        ns = (self.data.context.namespace
              if self.data is not None and getattr(self.data, "context", None)
              else _NS_DEFAULT)
        if self.data is None:
            self.last_msg = (
                f"[demo] scale {deploy_name} → {replicas} (sem cluster, no-op)"
            )
            self.last_ok = False
            return ActionResult.refresh()

        kubectl = kubectl_bin()
        if kubectl is None:
            self.last_msg = "kubectl não encontrado — scale impossível"
            self.last_ok = False
            return ActionResult.refresh()

        # Verifica se o deployment existe antes de tentar.
        check = subprocess.run(
            [kubectl, "-n", ns, "get", "deployment", deploy_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if check.returncode != 0:
            self.last_msg = (
                f"deployment/{deploy_name} não encontrado em `{ns}` — "
                "instale-o primeiro ([I] para claude-worker)"
            )
            self.last_ok = False
            return ActionResult.refresh()

        result = subprocess.run(
            [kubectl, "-n", ns, "scale",
             f"deployment/{deploy_name}", f"--replicas={replicas}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if result.returncode == 0:
            self.last_msg = f"deployment/{deploy_name} → {replicas} réplica(s)"
            self.last_ok = True
        else:
            self.last_msg = (
                f"scale {deploy_name} falhou: "
                f"{result.stderr.strip()[:120]}"
            )
            self.last_ok = False
        return ActionResult.refresh()

    def _handle_uninstall_confirm(self, key: str) -> ActionResult:
        """Roteia Y/N (+ enter sobre o cursor) no modal de uninstall."""
        if key in ("Y", "y"):
            self.mode = None
            self.picker_cursor = 0
            self._perform_uninstall()
            return ActionResult.refresh()
        if key in ("N", "n", "ESC"):
            self.mode = None
            self.picker_cursor = 0
            self.last_msg = "desinstalação cancelada"
            self.last_ok = None
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            if self.picker_cursor == 0:
                return self._handle_uninstall_confirm("Y")
            return self._handle_uninstall_confirm("N")
        if key in ("UP", "k", "DOWN", "j"):
            _, _, options = self.mode  # type: ignore[misc]
            n = len(options)
            if n:
                delta = -1 if key in ("UP", "k") else 1
                self.picker_cursor = (self.picker_cursor + delta) % n
            return ActionResult.refresh()
        return ActionResult()

    def _perform_uninstall(self, *, _blocking: bool = False) -> None:
        """Spawna :func:`uninstall_claude_worker` em thread daemon.

        Idêntica estratégia de :meth:`_perform_install` — kubectl deletes
        rodam em background pra não congelar o painel. Idempotente.

        :param _blocking: força inline (testes); default ``False`` (UX
            do painel nunca bloqueia).
        """
        import threading  # noqa: PLC0415

        if self._install_in_progress or (
                self._install_thread is not None
                and self._install_thread.is_alive()):
            self.last_msg = (
                "operação em andamento — aguarde antes de desinstalar"
            )
            self.last_ok = None
            return

        ns = (self.data.context.namespace
              if self.data is not None and getattr(self.data, "context", None)
              else _NS_DEFAULT)
        if (self.data is not None
                and getattr(self.data, "context", None) is None):
            ns = getattr(self.data, "namespace", None) or _NS_DEFAULT

        self.last_msg = (
            "desinstalando claude-worker em background — UI permanece "
            "responsiva, resultado em alguns segundos…"
        )
        self.last_ok = None
        self._install_in_progress = True

        if _blocking or getattr(self, "_install_blocking", False):
            self._run_uninstall_blocking(namespace=ns)
            return

        self._install_thread = threading.Thread(
            target=self._run_uninstall_blocking,
            kwargs={"namespace": ns},
            name="claude-uninstall",
            daemon=True,
        )
        self._install_thread.start()

    def _run_uninstall_blocking(self, *, namespace: str) -> None:
        """Worker body — kubectl deletes síncronos + publica resultado."""
        from _claude_install import \
            uninstall_claude_worker as _direct_uninstall  # noqa: PLC0415

        try:
            import _claude_install  # noqa: PLC0415
            uninstall_fn = getattr(
                _claude_install, "uninstall_claude_worker",
                _direct_uninstall,
            )
        except ImportError:
            uninstall_fn = _direct_uninstall

        try:
            result = uninstall_fn(namespace=namespace)
        except Exception as exc:  # noqa: BLE001
            self.last_msg = (
                f"falha em uninstall_claude_worker: "
                f"{type(exc).__name__}: {exc}"
            )
            self.last_ok = False
            self._install_in_progress = False
            return

        if getattr(result, "ok", False):
            self.last_msg = (
                "claude-worker desinstalado com sucesso — re-instale com [I]"
            )
            self.last_ok = True
        else:
            self.last_msg = (
                f"uninstall retornou erro: "
                f"{getattr(result, 'error', None) or '(sem detalhe)'}"
            )
            self.last_ok = False

        if self.data is not None:
            try:
                self.data.stage_dispatch._cache.invalidate()  # noqa: SLF001
                self.data.stage_dispatch._status_cache.invalidate()  # noqa: SLF001
            except (AttributeError, TypeError):
                pass

        self._install_in_progress = False

    def _handle_install_confirm(self, key: str) -> ActionResult:
        """Roteia Y/N (+ enter sobre o cursor) no modal de install."""
        if key in ("Y", "y"):
            self.mode = None
            self.picker_cursor = 0
            self._perform_install(force_relogin=False)
            return ActionResult.refresh()
        if key in ("N", "n", "ESC"):
            self.mode = None
            self.picker_cursor = 0
            self.last_msg = "instalação cancelada"
            self.last_ok = None
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            # Confirma via enter SE o cursor estiver na opção "Sim";
            # se estiver em "Cancelar", trata como N.
            if self.picker_cursor == 0:
                return self._handle_install_confirm("Y")
            return self._handle_install_confirm("N")
        if key in ("UP", "k", "DOWN", "j"):
            # Movimentação dentro das 2 opções (Sim/Cancelar).
            _, _, options = self.mode  # type: ignore[misc]
            n = len(options)
            if n:
                delta = -1 if key in ("UP", "k") else 1
                self.picker_cursor = (self.picker_cursor + delta) % n
            return ActionResult.refresh()
        return ActionResult()

    def _handle_switch_login_confirm(self, key: str) -> ActionResult:
        """Roteia Y/N (+ enter sobre o cursor) no modal de switch login."""
        if key in ("Y", "y"):
            self.mode = None
            self.picker_cursor = 0
            self._perform_install(force_relogin=True)
            return ActionResult.refresh()
        if key in ("N", "n", "ESC"):
            self.mode = None
            self.picker_cursor = 0
            self.last_msg = "troca de conta cancelada"
            self.last_ok = None
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            if self.picker_cursor == 0:
                return self._handle_switch_login_confirm("Y")
            return self._handle_switch_login_confirm("N")
        if key in ("UP", "k", "DOWN", "j"):
            _, _, options = self.mode  # type: ignore[misc]
            n = len(options)
            if n:
                delta = -1 if key in ("UP", "k") else 1
                self.picker_cursor = (self.picker_cursor + delta) % n
            return ActionResult.refresh()
        return ActionResult()

    def _handle_scale_prompt_key(self, key: str) -> ActionResult:
        """Roteia teclas no modal de escala numérica (0-10 réplicas).

        ↑/↓: navega nas opções numéricas.
        Enter: aplica kubectl scale.
        ESC/N: cancela.
        """
        if self.mode is None:
            return ActionResult()
        _kind, deploy_name, options = self.mode

        if key in ("N", "n", "ESC"):
            self.mode = None
            self.picker_cursor = 0
            self.last_msg = "escala cancelada"
            self.last_ok = None
            return ActionResult.refresh()

        if key in ("UP", "k"):
            n = len(options)
            self.picker_cursor = (self.picker_cursor - 1) % n if n else 0
            return ActionResult.refresh()

        if key in ("DOWN", "j"):
            n = len(options)
            self.picker_cursor = (self.picker_cursor + 1) % n if n else 0
            return ActionResult.refresh()

        if key in ("\r", "\n"):
            try:
                replicas = int(options[self.picker_cursor])
            except (IndexError, ValueError):
                replicas = 1
            self.mode = None
            self.picker_cursor = 0
            return self._apply_scaling(str(deploy_name), replicas)

        return ActionResult()

    def _handle_numeric_prompt_key(self, key: str) -> ActionResult:
        """Input handler for free-form numeric prompts (timeout / retries).

        ``self.mode = (kind, stage, [buffer])`` where ``buffer`` is the
        current digit string being edited. Confirms on [enter], cancels on
        [ESC]. Empty [enter] = clear the override (None).
        """
        if self.mode is None:
            return ActionResult()
        kind, stage, options = self.mode
        buf = options[0] if options else ""

        if key == "ESC":
            self.mode = None
            self.picker_cursor = 0
            self.last_msg = "entrada cancelada"
            self.last_ok = None
            return ActionResult.refresh()

        if key == "BACKSPACE" or key == "\x7f":
            new_buf = buf[:-1]
            self.mode = (kind, stage, [new_buf])
            return ActionResult.refresh()

        if key.isdigit():
            new_buf = buf + key
            self.mode = (kind, stage, [new_buf])
            return ActionResult.refresh()

        # '!' = force flag para retries=0 (semantic zero confirmado).
        # Sem efeito para timeout (timeout=0 sempre inválido). Idempotente:
        # repetir '!' não duplica no buffer — evita ``"0!!"`` que parsaria
        # como inteiro inválido e renderia mensagem confusa. Feedback do
        # review na PR #407.
        if key == "!" and kind == "retries":
            if buf.endswith("!"):
                return ActionResult()
            new_buf = buf + "!"
            self.mode = (kind, stage, [new_buf])
            return ActionResult.refresh()

        if key in ("\r", "\n"):
            ns = (self.data.context.namespace
                  if self.data is not None and getattr(self.data, "context", None)
                  else _NS_DEFAULT)
            self.mode = None
            self.picker_cursor = 0
            if self.data is None:
                self.last_msg = f"[demo] {kind}={buf!r} para '{stage}' (sem cluster, no-op)"
                self.last_ok = False
                return ActionResult.refresh()
            # Force flag para retries=0: buf == "0!" → allow_zero=True.
            force_zero = False
            raw_buf = buf.strip()
            if kind == "retries" and raw_buf.endswith("!"):
                force_zero = True
                raw_buf = raw_buf[:-1].strip()
            # Empty input = clear override.
            value: Optional[int] = None
            if raw_buf:
                try:
                    value = int(raw_buf)
                except ValueError:
                    self.last_msg = f"valor inválido {buf!r} — esperado inteiro"
                    self.last_ok = False
                    return ActionResult.refresh()
            if kind == "timeout":
                if value is not None and value <= 0:
                    self.last_msg = "timeout deve ser > 0"
                    self.last_ok = False
                    return ActionResult.refresh()
                ok, msg = pd_set_stage_timeout(stage, value, namespace=ns)
            else:  # retries
                if value is not None and value < 0:
                    self.last_msg = "retries deve ser >= 0"
                    self.last_ok = False
                    return ActionResult.refresh()
                ok, msg = pd_set_stage_retries(
                    stage, value, allow_zero=force_zero, namespace=ns,
                )
            self.last_ok = ok
            self.last_msg = msg
            # Invalida cache para render mostrar o novo valor.
            if self.data is not None:
                try:
                    self.data.stage_dispatch._cache.invalidate()  # noqa: SLF001
                    self.data.stage_dispatch._status_cache.invalidate()  # noqa: SLF001
                except (AttributeError, TypeError):
                    pass
            return ActionResult.refresh()

        return ActionResult()

    def _perform_install(self, *, force_relogin: bool,
                         _blocking: bool = False) -> None:
        """Spawna :func:`bootstrap_claude_worker` em thread daemon e retorna
        imediatamente.

        :param _blocking: força execução inline (usado por
            :meth:`_on_worker_selected` que precisa verificar
            ``cw_status.deployment_applied`` após o install, e por testes
            que dependem da assertion ser síncrona). Default ``False`` —
            UX do painel ([I]/[L] modals) nunca bloqueia.

        ``bootstrap_claude_worker`` chama ``subprocess.run(["claude", "auth",
        "login"], timeout=300)`` que bloqueia o caller até 5 min. O painel
        TUI tem event loop síncrono — qualquer chamada bloqueante congela
        ESC, refresh e qualquer tecla. Solução: rodar em thread daemon,
        publicar resultado em ``last_msg``/``last_ok`` quando termina, e o
        render normal mostra "executando…" → resultado naturalmente no
        próximo tick.

        Idempotente: se já há thread rodando, devolve mensagem informativa
        e ignora (evita spawn duplo se o operador apertar [I]/[L] de novo).

        Importação lazy + indireção pelo módulo (não pelo símbolo) para
        que o monkeypatch dos testes pegue a substituição em
        ``infra.k8s._claude_install.bootstrap_claude_worker``. O modo
        ``_install_blocking`` (testes) executa inline preservando o
        contrato anterior (sem thread) para os asserts continuarem válidos.
        """
        import threading  # noqa: PLC0415 — import lazy só quando necessário

        # Idempotência: ignora call concorrente enquanto thread anterior
        # ainda viva. ``_install_thread`` setado em spawn anterior; ``is_alive``
        # robusto a thread já joined (devolve False).
        if self._install_in_progress or (
                self._install_thread is not None
                and self._install_thread.is_alive()):
            self.last_msg = (
                "instalação/login do claude-worker já em andamento — "
                "aguarde o resultado antes de tentar de novo"
            )
            self.last_ok = None
            return

        ns = (self.data.context.namespace
              if self.data is not None and getattr(self.data, "context", None)
              else _NS_DEFAULT)
        # Em mocks (testes) ``self.data.namespace`` pode estar setado em
        # vez de ``context.namespace`` — fallback resiliente.
        if (self.data is not None
                and getattr(self.data, "context", None) is None):
            ns = getattr(self.data, "namespace", None) or _NS_DEFAULT

        action_kind = "switch-login" if force_relogin else "install"
        self.last_msg = (
            f"executando {action_kind} do claude-worker em background "
            f"(force_relogin={force_relogin}) — UI permanece responsiva, "
            f"resultado aparecerá aqui em alguns segundos…"
        )
        self.last_ok = None
        self._install_in_progress = True

        # Modo blocking: _on_worker_selected (verifica status após install)
        # e testes que dependem da chamada ser síncrona. Em produção o
        # painel ([I]/[L] modals) sempre passa _blocking=False.
        if _blocking or getattr(self, "_install_blocking", False):
            self._run_install_blocking(force_relogin=force_relogin,
                                       namespace=ns)
            return

        # Thread daemon — não impede o painel de fechar via [q].
        self._install_thread = threading.Thread(
            target=self._run_install_blocking,
            kwargs={"force_relogin": force_relogin, "namespace": ns},
            name=f"claude-{action_kind}",
            daemon=True,
        )
        self._install_thread.start()

    def _run_install_blocking(self, *, force_relogin: bool,
                              namespace: str) -> None:
        """Worker body — executa o bootstrap síncrono e publica resultado.

        Roda em :class:`threading.Thread` daemon spawnada por
        :meth:`_perform_install`. Atualiza ``last_msg``/``last_ok``/
        ``_install_in_progress`` ao final. Cache invalidation roda aqui
        para o próximo render do painel mostrar o estado novo sem [r]
        manual.

        Thread safety: atribuições de string/bool a atributos de instância
        são atomic em CPython (GIL). Pior caso: tick do render lê uma
        atualização parcial — ele simplesmente re-lê no próximo tick (1s).
        """
        from _claude_install import \
            bootstrap_claude_worker as _direct_bootstrap  # noqa: PLC0415

        try:
            import _claude_install  # noqa: PLC0415
            bootstrap_fn = getattr(
                _claude_install, "bootstrap_claude_worker", _direct_bootstrap,
            )
        except ImportError:
            bootstrap_fn = _direct_bootstrap

        try:
            result = bootstrap_fn(
                namespace=namespace,
                force_relogin=force_relogin,
                interactive=True,
                # Painel TUI: jamais inherit stdio do subprocess do claude,
                # senão o display Rich é corrompido pelo OAuth URL/prompts.
                # Output do claude é capturado e logado via logger.info.
                inherit_stdio=False,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_msg = (
                f"falha em bootstrap_claude_worker: "
                f"{type(exc).__name__}: {exc}"
            )
            self.last_ok = False
            self._install_in_progress = False
            return

        if not getattr(result, "ok", False):
            self.last_msg = (
                f"bootstrap retornou erro: "
                f"{getattr(result, 'error', None) or '(sem detalhe)'}"
            )
            self.last_ok = False
        else:
            email = getattr(result, "account_email", None) or "?"
            verb = "logado novamente" if force_relogin else "instalado"
            self.last_msg = (
                f"claude-worker {verb} com sucesso "
                f"(conta: {email})"
            )
            self.last_ok = True

        # Cache invalidation — força re-fetch no próximo render.
        if self.data is not None:
            try:
                self.data.stage_dispatch._cache.invalidate()  # noqa: SLF001
                self.data.stage_dispatch._status_cache.invalidate()  # noqa: SLF001
            except (AttributeError, TypeError):
                # MagicMock pode não ter o atributo — ignora.
                pass

        # Libera o slot por último — render observa estado completo antes.
        self._install_in_progress = False

    def _on_worker_selected(self, stage: str, choice: str) -> None:
        """Hook chamado quando o operador escolhe um worker no picker.

        Se ``choice == 'claude-worker'`` e o Deployment ainda não foi
        aplicado, dispara o install antes de persistir a configuração
        per-stage — evita o caso "operador escolhe claude-worker e o
        pipeline tenta despachar pra um pod inexistente".

        Em qualquer outro caso, delega para
        :func:`set_pipeline_dispatch_stage`.
        """
        ns = (self.data.context.namespace
              if self.data is not None and getattr(self.data, "context", None)
              else _NS_DEFAULT)
        if (self.data is not None
                and getattr(self.data, "context", None) is None):
            ns = getattr(self.data, "namespace", None) or _NS_DEFAULT

        if choice == "claude-worker":
            cw_status = self._claude_status()
            if not cw_status.deployment_applied:
                # Install antes de persistir. Em modo interativo do painel
                # o ideal seria um modal — para a Task 20 chamamos direto
                # (testes monkeypatcham bootstrap; uso real do painel
                # passa pelo modal [I] antes de chegar aqui na maioria dos
                # casos. Quando o operador pula direto pro picker, este
                # branch garante consistência).
                # _blocking=True: precisamos do cw_status atualizado já
                # na próxima linha; spawnar thread aqui criaria race.
                self._perform_install(force_relogin=False, _blocking=True)
                cw_status = self._claude_status()
                if not cw_status.deployment_applied:
                    self.last_msg = (
                        "install do claude-worker falhou — worker não "
                        "persistido"
                    )
                    self.last_ok = False
                    return

        # Persistir override per-stage.
        if choice == self._CLEAR_SENTINEL_WORKER:
            ok, msg = pd_set_pipeline_dispatch_stage(
                stage, None, namespace=ns,
            )
        else:
            ok, msg = pd_set_pipeline_dispatch_stage(
                stage, choice, namespace=ns,
            )
        self.last_ok = ok
        self.last_msg = msg

    # --- picker key handler ---------------------------------------------

    def _handle_picker_key(self, key: str) -> ActionResult:
        """Roteia teclas quando ``self.mode`` está ativo.

        - ESC → fecha o modal sem aplicar (cancela).
        - ↑/↓ → navega na lista de opções.
        - enter → confirma seleção, chama :meth:`_apply_picker_selection`,
          fecha o modal e invalida o cache do provider.
        - install_confirm / switch_login_confirm / uninstall_confirm →
          handlers dedicados (Y/N + enter sobre cursor).
        - scale_prompt → handler de escala numérica (0-10 réplicas).
        """
        if self.mode is not None and self.mode[0] == "install_confirm":
            return self._handle_install_confirm(key)
        if self.mode is not None and self.mode[0] == "switch_login_confirm":
            return self._handle_switch_login_confirm(key)
        if self.mode is not None and self.mode[0] == "uninstall_confirm":
            return self._handle_uninstall_confirm(key)
        if self.mode is not None and self.mode[0] == "scale_prompt":
            return self._handle_scale_prompt_key(key)
        if self.mode is not None and self.mode[0] in ("timeout", "retries"):
            return self._handle_numeric_prompt_key(key)
        if self.mode is not None and self.mode[0] == "cleanup_confirm":
            return self._handle_cleanup_confirm_key(key)
        if self.mode is not None and self.mode[0] == "max_parallel_prompt":
            return self._handle_max_parallel_prompt_key(key)
        if key == "ESC":
            self.mode = None
            self.picker_cursor = 0
            return ActionResult.refresh()
        if self.mode is None:  # defesa — não deveria chegar aqui
            return ActionResult()
        _kind, _stage, options = self.mode
        n = len(options)
        if n == 0:
            # Nada a navegar — qualquer tecla fecha.
            if key in ("\r", "\n"):
                self.mode = None
            return ActionResult()
        if key in ("UP", "k"):
            self.picker_cursor = (self.picker_cursor - 1) % n
            return ActionResult.refresh()
        if key in ("DOWN", "j"):
            self.picker_cursor = (self.picker_cursor + 1) % n
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            self._apply_picker_selection()
            self.mode = None
            self.picker_cursor = 0
            # Invalida cache do StageDispatchProvider para a próxima
            # render mostrar o valor novo (sem precisar de [r] manual).
            if self.data is not None:
                try:
                    self.data.stage_dispatch._cache.invalidate()  # noqa: SLF001
                    self.data.stage_dispatch._status_cache.invalidate()  # noqa: SLF001
                except (AttributeError, TypeError):
                    # MagicMock pode não ter o atributo — ignora.
                    pass
            return ActionResult.refresh()
        return ActionResult()

    # --- apply (writes) --------------------------------------------------

    def _apply_picker_selection(self) -> None:
        """Despacha a seleção corrente do picker para o helper apropriado.

        Cada branch espelha o callsite equivalente em :class:`StageModelsView`
        / :class:`DispatchModeView`. Em modo demo (``data=None``) só
        registra ``last_msg`` sem chamar kubectl.
        """
        if self.mode is None:
            return
        kind, stage, options = self.mode
        selected = options[self.picker_cursor]
        ns = (self.data.context.namespace
              if self.data is not None and getattr(self.data, "context", None)
              else _NS_DEFAULT)
        if self.data is None:
            self.last_msg = f"[demo] {kind}: '{selected}' (sem cluster, no-op)"
            self.last_ok = False
            return

        # --- per-stage worker -------------------------------------------
        if kind == "worker":
            # Delega a :meth:`_on_worker_selected` para reusar o hook de
            # install-on-the-fly quando ``selected == 'claude-worker'``
            # e o Deployment ainda não foi aplicado (Task 20).
            self._on_worker_selected(stage, selected)
            return

        # --- per-stage model --------------------------------------------
        if kind == "model":
            if selected == self._CLEAR_SENTINEL_MODEL:
                ok, msg = pd_clear_stage_model(stage)
            else:
                ok, msg = pd_set_stage_model(stage, selected)
            self.last_ok = ok
            self.last_msg = msg
            return

        # --- global worker (DEILE_PIPELINE_DISPATCH_MODE) ---------------
        if kind == "global_worker":
            if selected == "(clear override)":
                ok, msg = pd_clear_pipeline_dispatch_mode(namespace=ns)
            else:
                # set_pipeline_dispatch_mode espera "claude" | "deile_worker"
                # (conjunto canônico do _DISPATCH_MODES_ALLOWED). O picker
                # mostra "deile-worker" / "claude-worker" (hyphen, paridade
                # com o per-stage). Mapeia hyphen→underscore aqui.
                mode_for_global = (
                    "deile_worker" if selected == "deile-worker"
                    else "claude" if selected == "claude-worker"
                    else selected
                )
                ok, msg = pd_set_pipeline_dispatch_mode(
                    mode_for_global, namespace=ns,
                )
            self.last_ok = ok
            self.last_msg = msg
            return

        # --- per-stage cost cap (issue #392) ----------------------------
        if kind == "cost_cap_usd":
            if selected == "(no cap)":
                ok, msg = pd_reset_stage_cost_cap_usd(stage, namespace=ns)
            else:
                ok, msg = pd_set_stage_cost_cap_usd(stage, selected,
                                                    namespace=ns)
            self.last_ok = ok
            self.last_msg = msg
            return

        # --- global model (DEILE_PREFERRED_MODEL em deile-worker) -------
        if kind == "global_model":
            if selected == "(clear override)":
                # ``set_preferred_model`` não tem variante clear; o operador
                # usa o reset action do Task 19 (próximo PR). Por ora,
                # avisa que clear global ainda não está wired.
                self.last_ok = False
                self.last_msg = (
                    "clear de DEILE_PREFERRED_MODEL ainda não implementado "
                    "— use [r] na célula (Task 19 — reset)"
                )
                return
            ok, msg = pd_set_preferred_model(
                "deile-worker", selected, namespace=ns,
            )
            self.last_ok = ok
            self.last_msg = msg
            return

        # --- timeout / retries (handled by _handle_numeric_prompt_key;
        # this branch is a fallback in case _apply_picker_selection is called
        # directly without going through the numeric handler) ---------------
        if kind in ("timeout", "retries"):
            # Already handled inline in _handle_numeric_prompt_key.
            # No-op here; defensive against unexpected call paths.
            return

    # --- cleanup on-demand (issue #408) ---------------------------------

    def _get_cleanup_preview(self) -> str:
        """Gera texto de preview do que seria removido pelo cleanup.

        Lê via ``kubectl exec`` no primeiro pod claude-worker disponível.
        Em modo demo ou sem kubectl → retorna mensagem explicativa.
        """
        ns = (self.data.context.namespace
              if self.data is not None and getattr(self.data, "context", None)
              else _NS_DEFAULT)
        if self.data is None:
            return "[demo] sem cluster — cleanup simulado"
        kubectl = kubectl_bin()
        if kubectl is None:
            return "kubectl não encontrado — preview indisponível"
        # Encontra o primeiro pod claude-worker Running.
        try:
            result = subprocess.run(
                [kubectl, "-n", ns, "get", "pods",
                 "-l", "app=claude-worker",
                 "--field-selector=status.phase=Running",
                 "-o", "jsonpath={.items[0].metadata.name}"],
                capture_output=True, text=True, timeout=10,
            )
            pod_name = result.stdout.strip()
        except Exception:  # noqa: BLE001
            return "erro ao listar pods claude-worker"
        if not pod_name:
            return "nenhum pod claude-worker Running — cleanup via CronJob apenas"
        # Executa dry-run do cleanup dentro do pod.
        dry_run_cmd = (
            "python3 -c \""
            "import sys, os; sys.path.insert(0, '/app/infra/k8s'); "
            "from claude_worker_server import startup_cleanup, _CLEANUP_RETENTION_DAYS; "
            "import time, json; "
            "from pathlib import Path; "
            "root = Path(os.environ.get('DEILE_CLAUDE_WORKER_ROOT', '/home/claude/work')); "
            "dirs = [d for d in root.iterdir() if d.is_dir() and d.name.isalnum() and len(d.name)==16] if root.is_dir() else []; "
            "print(f'PVC root: {root}'); "
            "print(f'Total workdirs: {len(dirs)}'); "
            "\""
        )
        try:
            result = subprocess.run(
                [kubectl, "-n", ns, "exec", pod_name,
                 "--", "sh", "-c", dry_run_cmd],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return result.stdout.strip() or "(sem output do pod)"
            return f"exec falhou (rc={result.returncode}): {result.stderr.strip()[:200]}"
        except Exception as exc:  # noqa: BLE001
            return f"erro exec: {exc}"

    def _open_cleanup_confirm(self) -> ActionResult:
        """Abre modal de confirmação do cleanup on-demand com preview."""
        preview = self._get_cleanup_preview()
        self.mode = ("cleanup_confirm", preview, ["Sim (Y)", "Não (N)"])
        self.picker_cursor = 1  # default: não
        self.last_msg = ""
        self.last_ok = None
        return ActionResult.refresh()

    def _handle_cleanup_confirm_key(self, key: str) -> ActionResult:
        """Roteia Y/N no modal de cleanup."""
        if key in ("Y", "y"):
            self.mode = None
            self.picker_cursor = 0
            return self._run_cleanup_via_kubectl()
        if key in ("N", "n", "ESC"):
            self.mode = None
            self.picker_cursor = 0
            self.last_msg = "cleanup cancelado"
            self.last_ok = None
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            return self._handle_cleanup_confirm_key(
                "Y" if self.picker_cursor == 0 else "N"
            )
        if key in ("UP", "k", "DOWN", "j"):
            _, _, options = self.mode  # type: ignore[misc]
            n = len(options)
            if n:
                delta = -1 if key in ("UP", "k") else 1
                self.picker_cursor = (self.picker_cursor + delta) % n
            return ActionResult.refresh()
        return ActionResult()

    def _run_cleanup_via_kubectl(self) -> ActionResult:
        """Executa cleanup real via kubectl exec no primeiro pod claude-worker."""
        ns = (self.data.context.namespace
              if self.data is not None and getattr(self.data, "context", None)
              else _NS_DEFAULT)
        if self.data is None:
            self.last_msg = "[demo] cleanup simulado (sem cluster)"
            self.last_ok = False
            return ActionResult.refresh()
        kubectl = kubectl_bin()
        if kubectl is None:
            self.last_msg = "kubectl não encontrado — cleanup impossível"
            self.last_ok = False
            return ActionResult.refresh()
        try:
            result = subprocess.run(
                [kubectl, "-n", ns, "get", "pods",
                 "-l", "app=claude-worker",
                 "--field-selector=status.phase=Running",
                 "-o", "jsonpath={.items[0].metadata.name}"],
                capture_output=True, text=True, timeout=10,
            )
            pod_name = result.stdout.strip()
        except Exception as exc:  # noqa: BLE001
            self.last_msg = f"erro ao listar pods: {exc}"
            self.last_ok = False
            return ActionResult.refresh()
        if not pod_name:
            self.last_msg = "nenhum pod claude-worker Running — cleanup indisponível"
            self.last_ok = False
            return ActionResult.refresh()
        cleanup_cmd = (
            "python3 -c \""
            "import sys, os; sys.path.insert(0, '/app/infra/k8s'); "
            "from claude_worker_server import startup_cleanup; "
            "r = startup_cleanup(); "
            "print(f\\\"leases={r[\\\"leases_removed\\\"]} workdirs={r[\\\"workdirs_removed\\\"]} freed={r[\\\"bytes_freed\\\"]}B errors={len(r[\\\"errors\\\"])}\\\"); "
            "\""
        )
        try:
            result = subprocess.run(
                [kubectl, "-n", ns, "exec", pod_name,
                 "--", "sh", "-c", cleanup_cmd],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                self.last_msg = f"cleanup OK: {result.stdout.strip()[:200]}"
                self.last_ok = True
            else:
                self.last_msg = f"cleanup falhou: {result.stderr.strip()[:200]}"
                self.last_ok = False
        except Exception as exc:  # noqa: BLE001
            self.last_msg = f"erro cleanup: {exc}"
            self.last_ok = False
        return ActionResult.refresh()

    # --- max_parallel (issue #408) --------------------------------------

    def _read_max_parallel_env(self) -> str:
        """Lê DEILE_PIPELINE_MAX_PARALLEL atual do Deployment deile-pipeline.

        Retorna string vazia se não conseguir ler (kubectl indisponível,
        deployment não encontrado, env var não setada).
        """
        ns = (self.data.context.namespace
              if self.data is not None and getattr(self.data, "context", None)
              else _NS_DEFAULT)
        if self.data is None:
            return ""
        kubectl = kubectl_bin()
        if kubectl is None:
            return ""
        try:
            result = subprocess.run(
                [kubectl, "-n", ns, "get", "deployment", "deile-pipeline",
                 "-o",
                 "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='DEILE_PIPELINE_MAX_PARALLEL')].value}"],
                capture_output=True, text=True, timeout=8,
            )
            return result.stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    def _open_max_parallel_prompt(self) -> ActionResult:
        """Abre modal de edição numérica do max_parallel."""
        current = self._read_max_parallel_env() or ""
        self.mode = ("max_parallel_prompt", None, [current])
        self.picker_cursor = 0
        self.last_msg = ""
        self.last_ok = None
        return ActionResult.refresh()

    def _handle_max_parallel_prompt_key(self, key: str) -> ActionResult:
        """Captura entrada numérica ou 'auto' para DEILE_PIPELINE_MAX_PARALLEL."""
        _, stage, options = self.mode  # type: ignore[misc]
        buf: str = options[0] if options else ""

        if key == "ESC":
            self.mode = None
            self.picker_cursor = 0
            self.last_msg = "edição de max_parallel cancelada"
            self.last_ok = None
            return ActionResult.refresh()

        if key == "a":
            # Atalho: seta valor "auto" (deriva de réplicas claude-worker).
            buf = "auto"
            self.mode = ("max_parallel_prompt", stage, [buf])
            return ActionResult.refresh()

        if key == "BACKSPACE" or key == "\x7f":
            buf = buf[:-1]
            self.mode = ("max_parallel_prompt", stage, [buf])
            return ActionResult.refresh()

        if key in ("\r", "\n"):
            self.mode = None
            self.picker_cursor = 0
            if buf.strip() == "":
                # Limpa o override (remove env var).
                return self._apply_max_parallel(None)
            return self._apply_max_parallel(buf.strip())

        if key.isdigit():
            buf += key
            self.mode = ("max_parallel_prompt", stage, [buf])
            return ActionResult.refresh()

        return ActionResult()

    def _apply_max_parallel(self, value: Optional[str]) -> ActionResult:
        """Aplica DEILE_PIPELINE_MAX_PARALLEL no Deployment deile-pipeline.

        ``value=None`` → remove o override (kubectl set env VAR-).
        ``value="auto"`` → seta a string "auto" como sentinela.
        ``value="N"`` → seta valor numérico.
        """
        ns = (self.data.context.namespace
              if self.data is not None and getattr(self.data, "context", None)
              else _NS_DEFAULT)
        if self.data is None:
            self.last_msg = f"[demo] max_parallel → {value!r} (sem cluster, no-op)"
            self.last_ok = False
            return ActionResult.refresh()

        kubectl = kubectl_bin()
        if kubectl is None:
            self.last_msg = "kubectl não encontrado — max_parallel não alterado"
            self.last_ok = False
            return ActionResult.refresh()

        # Valida valor numérico (aceita "auto" e None como casos especiais).
        if value is not None and value != "auto":
            try:
                n = int(value)
                if n < 1:
                    raise ValueError(f"valor deve ser >= 1, got {n}")
            except ValueError as exc:
                self.last_msg = f"valor inválido para max_parallel: {exc}"
                self.last_ok = False
                return ActionResult.refresh()

        env_arg = (f"DEILE_PIPELINE_MAX_PARALLEL-"  # remove
                   if value is None
                   else f"DEILE_PIPELINE_MAX_PARALLEL={value}")
        try:
            result = subprocess.run(
                [kubectl, "-n", ns, "set", "env",
                 "deployment/deile-pipeline", env_arg],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                action = "removido (default: 2)" if value is None else f"→ {value!r}"
                self.last_msg = f"DEILE_PIPELINE_MAX_PARALLEL {action}"
                self.last_ok = True
            else:
                self.last_msg = (
                    f"set env falhou: {result.stderr.strip()[:150]}"
                )
                self.last_ok = False
        except Exception as exc:  # noqa: BLE001
            self.last_msg = f"erro ao setar max_parallel: {exc}"
            self.last_ok = False
        return ActionResult.refresh()


class StubView(View):
    """Sub-view placeholder enquanto a Fase correspondente não foi feita."""

    HOTKEYS = "[esc] back   [q] quit"

    def __init__(self, name: str, title: str, fase: str, what: str):
        self.name = name
        self.title = title
        self.fase = fase
        self.what = what

    def render(self, app: "PanelApp") -> RenderableType:
        body = Group(
            Align.center(Text(self.title, style="bold cyan"), vertical="middle"),
            Text(),
            Align.center(Text(f"Em construção — {self.fase}", style="yellow")),
            Text(),
            Align.center(Text(self.what, style="dim")),
            Text(),
            Align.center(Text("[esc] volta ao dashboard", style="dim")),
        )
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(body, border_style="dim"), name="body"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout


class HelpView(View):
    name = "help"
    title = "Ajuda"
    refresh_s = 60.0

    HOTKEYS = "[esc] back   [q] quit"

    def render(self, app: "PanelApp") -> RenderableType:
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, show_header=False)
        tbl.add_column("tecla", style="bold cyan", width=18)
        tbl.add_column("ação")
        rows = [
            ("1-5, a, m, d, n",  "drill em sub-view (no dashboard)"),
            ("m",             "modelo do deployment (runtime)"),
            ("d",             "pipeline stage configuration (workers & models por etapa)"),
            ("↑/↓ ou j/k",    "navega em listas (picker, issues/PRs, modelos)"),
            ("enter",         "seleciona o item destacado"),
            ("esc",           "volta à view anterior (ou ao dashboard)"),
            ("q",             "sai do painel"),
            ("p",             "pause / resume refresh automático"),
            ("+ / -",         "acelera / desacelera o refresh (×0.25 a ×4)"),
            ("r",             "força refresh imediato (invalida caches)"),
            ("s",             "snapshot: salva a tela atual em ~/.deile/snapshots/"),
            (".",             "Pod Watch: abre o arquivo de log em editor (cursor/code/…)"),
            ("?",             "esta tela"),
        ]
        for k, v in rows:
            tbl.add_row(k, v)
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(tbl, title="[bold]Hotkeys globais[/bold]",
                         title_align="left", border_style="cyan"),
                   name="body"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout


# ===== main app =============================================================

@dataclass
class _Settings:
    """Estado mutável compartilhado entre views e key handler."""

    paused: bool = False
    refresh_mult: float = 1.0    # 0.25, 0.5, 1.0, 2.0, 4.0
    snapshots_dir: Optional[str] = None
    # Fila simples de alertas para feedback do usuário (snapshot salvo,
    # clipboard indisponível, etc). Cada item: (icon, msg, expires_at).
    # Mostrado no header e expira após 5s. Não persiste entre runs.
    toasts: List[tuple] = field(default_factory=list)


class PanelApp:
    """Loop principal: rich.Live + key handler + view stack.

    O stack é uma pilha de views; navegação empilha e ESC desempilha.
    Cada view declara `refresh_s` que combinada com `refresh_mult` define
    a cadência efetiva.
    """

    def __init__(self, views: Dict[str, View], root: str = "dashboard",
                 data: Optional[PanelData] = None,
                 memdebug: bool = False):
        self.views = views
        self.stack: List[View] = [views[root]]
        self.running = True
        self.console = Console()
        self.settings = _Settings()
        self.data = data
        self.last_payload: Dict[str, Any] = {}
        self._last_render = 0.0
        # `--memdebug`: liga tracemalloc + amostragem periódica do top N.
        # Default OFF (não há overhead em uso normal). Quando ligado,
        # `_memdebug_line()` devolve a string que vai pro head do painel.
        self._memdebug = bool(memdebug)
        self._memdebug_last_sample_at: float = 0.0
        self._memdebug_summary: str = ""
        if self._memdebug:
            import tracemalloc  # noqa: PLC0415 — opt-in import
            if not tracemalloc.is_tracing():
                tracemalloc.start(10)  # 10 frames são suficientes p/ atribuir

    # --- propriedades de conveniência expostas às views ---

    @property
    def paused(self) -> bool:
        return self.settings.paused

    @property
    def refresh_mult(self) -> float:
        return self.settings.refresh_mult

    @property
    def current_view(self) -> View:
        return self.stack[-1]

    @property
    def current_refresh_s(self) -> float:
        return self.current_view.refresh_s / max(self.refresh_mult, 0.01)

    # --- navegação ---

    def push(self, name: str, **payload: Any) -> None:
        view = self.views.get(name)
        if view is None:
            return
        self.current_view.on_unmount(self)
        self.last_payload = payload
        self.stack.append(view)
        view.on_mount(self)

    def pop(self) -> None:
        if len(self.stack) <= 1:
            return
        self.current_view.on_unmount(self)
        self.stack.pop()
        self.current_view.on_mount(self)

    def quit(self) -> None:
        self.running = False

    # --- memdebug (--memdebug) -----------------------------------------
    # Quando ligado, faz `tracemalloc.snapshot()` a cada 60s e calcula
    # o crescimento desde o último snapshot. Mostra:
    #   "mem: cur 42.1MB · peak 51.3MB · Δ60s +1.2MB · top: <file>:<line>"
    # Off por default — overhead do tracemalloc é não-trivial.

    _MEMDEBUG_INTERVAL_S = 60.0

    def memdebug_line(self) -> str:
        if not self._memdebug:
            return ""
        now = time.monotonic()
        if now - self._memdebug_last_sample_at < self._MEMDEBUG_INTERVAL_S:
            return self._memdebug_summary
        try:
            import tracemalloc  # noqa: PLC0415
            cur, peak = tracemalloc.get_traced_memory()
            snap = tracemalloc.take_snapshot()
            # Top alocador por linha — útil pra identificar leak source.
            stats = snap.statistics("lineno")
            top_line = ""
            if stats:
                top = stats[0]
                # tracemalloc.StatisticDiff/Statistic.traceback[-1] é o frame
                # mais profundo (call site real).
                frame = top.traceback[-1]
                fpath = Path(frame.filename).name
                top_line = (f" · top: {fpath}:{frame.lineno} "
                            f"({top.size / 1024:.0f}KB)")
            # Delta vs último sample (se houver).
            delta_str = ""
            prev = getattr(self, "_memdebug_prev_cur", None)
            if prev is not None:
                delta = (cur - prev) / 1024
                sign = "+" if delta >= 0 else ""
                delta_str = f" · Δ{int(self._MEMDEBUG_INTERVAL_S)}s {sign}{delta:.0f}KB"
            self._memdebug_prev_cur = cur
            self._memdebug_summary = (
                f"mem: cur {cur / 1024 / 1024:.1f}MB · "
                f"peak {peak / 1024 / 1024:.1f}MB{delta_str}{top_line}"
            )
            self._memdebug_last_sample_at = now
        except Exception as exc:  # noqa: BLE001 — memdebug nunca quebra UI
            self._memdebug_summary = f"mem: erro tracemalloc ({exc})"
            self._memdebug_last_sample_at = now
        return self._memdebug_summary

    # --- snapshots ---

    def _snapshot(self) -> Optional[str]:
        out_dir = Path(
            self.settings.snapshots_dir
            or os.path.expanduser("~/.deile/snapshots")
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"panel-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        capture = Console(record=True, width=self.console.size.width)
        capture.print(self.current_view.render(self))
        path.write_text(capture.export_text(), encoding="utf-8")
        self._purge_old_snapshots(out_dir)
        return str(path)

    @staticmethod
    def _purge_old_snapshots(out_dir: Path) -> None:
        """Mantém só os `_SNAPSHOT_RETAIN` mais recentes (por mtime).

        Sem política, ~/.deile/snapshots/ cresce indefinidamente."""
        try:
            files = sorted(
                (p for p in out_dir.glob("panel-*.txt") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for old in files[_SNAPSHOT_RETAIN:]:
            try:
                old.unlink()
            except OSError:
                pass

    def push_toast(self, icon: str, msg: str, ttl_s: float = 5.0) -> None:
        """Adiciona um toast efêmero (mostrado no header até expirar).

        Dedup: se já existe toast com a mesma `msg`, apenas renova o
        timestamp em vez de empilhar — evita que apertar [s] N vezes
        acumule N toasts idênticos.
        """
        expires_at = time.monotonic() + ttl_s
        for i, (existing_icon, existing_msg, _) in enumerate(
            self.settings.toasts,
        ):
            if existing_msg == msg:
                self.settings.toasts[i] = (existing_icon, existing_msg,
                                           expires_at)
                return
        self.settings.toasts.append((icon, msg, expires_at))

    def active_toasts(self) -> List[tuple]:
        """Retorna toasts não-expirados (ícone, mensagem). Limpa expirados.

        Fast-path: se nenhum toast expirou, devolve a projeção direta sem
        reconstruir a lista interna (chamado em ~30fps pelo renderer).
        """
        toasts = self.settings.toasts
        now = time.monotonic()
        if all(t[2] > now for t in toasts):
            return [(i, m) for i, m, _ in toasts]
        self.settings.toasts = [t for t in toasts if t[2] > now]
        return [(i, m) for i, m, _ in self.settings.toasts]

    # --- key dispatch ---

    def _handle_global(self, key: str) -> bool:
        """Hotkeys globais que TODA view respeita.

        Retorna True quando consumiu a tecla — a view não vê o key.
        """
        if key == "q":
            self.quit()
            return True
        if key == "ESC":
            self.pop()
            return True
        if key == "p":
            self.settings.paused = not self.settings.paused
            return True
        if key == "+":
            self.settings.refresh_mult = min(self.settings.refresh_mult * 2, 4.0)
            return True
        if key == "-":
            self.settings.refresh_mult = max(self.settings.refresh_mult / 2, 0.25)
            return True
        if key == "r":
            # PodPickerView reivindica `r` como "rollout restart do pod
            # selecionado". Cedemos a tecla pra view — o operador ainda
            # pode forçar refresh via `+`/`-` (cadência) ou aguardar o
            # próximo tick. Demais views mantêm o comportamento original.
            if self.current_view.name == "pod-picker":
                return False
            self._last_render = 0.0       # força próximo tick a renderizar
            if self.data is not None:
                self.data.force_refresh_all()
            return True
        if key == "s":
            try:
                path = self._snapshot()
            except OSError as exc:
                self.push_toast("⚠", f"snapshot falhou: {exc}", ttl_s=6.0)
                return True
            if path:
                self.push_toast("💾", f"snapshot salvo: {path}", ttl_s=6.0)
            return True
        return False

    # --- main loop ---

    def run(self) -> int:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            self.console.print(
                "[yellow]painel exige terminal interativo "
                "(stdin e stdout precisam ser TTY).[/yellow]"
            )
            return 1
        # BackgroundRefresher mantém os caches frescos fora do thread
        # principal — sem isso, `render()` que cair num provider com TTL
        # vencido bloqueia a UI por segundos (kubectl/gh).
        refresher = (BackgroundRefresher(self.data)
                     if self.data is not None else None)
        if refresher is not None:
            refresher.start()
        try:
            return self._run_loop(refresher)
        finally:
            if refresher is not None:
                refresher.stop()

    def _run_loop(self, refresher: Optional["BackgroundRefresher"]) -> int:
        with KeyReader() as keys, Live(
            self.current_view.render(self),
            console=self.console,
            screen=True,
            # 10 FPS é suficiente pra UI fluida (TUI não é jogo). 30 FPS
            # mantinha o render-pipeline do Rich quente o tempo todo,
            # acumulando alocações em sessões longas (relato do operador:
            # Cursor matava o processo após horas com painel aberto).
            # Combinado com BackgroundRefresher.DEFAULT_TICK_S=1.0, reduz
            # ~3x a CPU sustained do painel.
            refresh_per_second=10,
            transient=False,
        ) as live:
            self._last_render = time.monotonic()
            while self.running:
                # `timeout=0.05` mantém o loop responsivo (até 50ms para
                # detectar tecla nova) sem ficar acordando inutilmente.
                key = keys.read(timeout=0.05)
                consumed = False
                if key:
                    # Views can short-circuit a global hotkey (e.g. ESC inside
                    # a modal must close the modal before global ESC pops view).
                    if self.current_view.intercepts_key(key):
                        result = self.current_view.handle_key(key, self)
                        self._apply(result)
                        if result.kind != Action.NOOP:
                            consumed = True
                    elif self._handle_global(key):
                        consumed = True
                    else:
                        result = self.current_view.handle_key(key, self)
                        self._apply(result)
                        # NOOP = tecla ignorada pela view; não força render.
                        if result.kind != Action.NOOP:
                            consumed = True
                if not self.running:
                    break
                # Render imediato quando: (a) tecla mudou o estado,
                # (b) hotkey [r] zerou `_last_render`, ou (c) o
                # `refresh_s` da view vigente venceu.
                now = time.monotonic()
                cadence_due = (
                    not self.paused
                    and (now - self._last_render) >= self.current_refresh_s
                )
                if consumed or self._last_render == 0.0 or cadence_due:
                    live.update(self.current_view.render(self))
                    self._last_render = time.monotonic()
        return 0

    def _apply(self, result: ActionResult) -> None:
        if result.kind == Action.QUIT:
            self.quit()
        elif result.kind == Action.BACK:
            self.pop()
        elif result.kind == Action.NAV and result.target:
            self.push(result.target, **result.payload)
        elif result.kind == Action.REFRESH:
            self._last_render = 0.0


# ===== entry point ==========================================================

def _build_views(data: Optional[PanelData] = None) -> Dict[str, View]:
    """Registry das views. Próximas fases trocam os stubs por views reais."""
    return {
        "dashboard": DashboardView(data=data),
        "help": HelpView(),
        "pod-picker": PodPickerView(data=data),
        "pod-watch": PodWatchView(data=data),
        "pipeline-timeline": PipelineTimelineView(data=data),
        "issues-prs": IssuesPRsView(data=data),
        "logs-split": StubView(
            "logs-split", "Logs Split", "Pós-MVP",
            "Pipeline + Worker-1 + Worker-2 lado a lado, follow real. "
            "Pode ser composto a partir do _LogStreamer.",
        ),
        "tokens": TokensView(data=data),
        "actions": ActionsView(data=data),
        "notifier-echo": NotifierEchoView(data=data),
        "model-switcher": ModelSwitcherView(data=data),
        # Issue #309 fase 2 — Task 21 cutover. A matriz unificada substitui
        # ``DispatchModeView`` (PR #330, key ``dispatch-mode``) e
        # ``StageModelsView`` (#305, key ``stage-models``) — ambas saíram do
        # registry mas as classes ainda existem no módulo (FU cleanup).
        "dispatch-mode-matrix": DispatchMatrixView(data=data),
    }


def run_panel(context: "Optional[Any]" = None,
              force_demo: bool = False,
              memdebug: bool = False) -> int:
    """Entry point chamado pelo `deploy.py panel`.

    Levanta os providers reais. Suporta 3 modos:

    1. **K8s + local** (default): detecta automaticamente kubectl/cluster
       e processos locais, mostra tudo lado a lado.
    2. **Forçado** via `context` (com `k8s_force=True` ou `local_force=True`).
    3. **Demo** (`force_demo=True`): mocks puros, útil quando nada está
       no ar e o operador quer ver a UX.

    Diferente da versão anterior, **não cai em demo automaticamente** —
    se k8s estiver fora mas há logs locais, o painel renderiza só os
    locais (modo "local only"). Demo agora exige opt-in explícito.
    """
    # Import local para evitar import circular no topo.
    from _panel_data import RuntimeContext  # noqa: PLC0415
    from _panel_data import discover_deile_namespaces  # noqa: PLC0415

    if force_demo:
        data: Optional[PanelData] = None
    else:
        # Quando o operador não especificou --namespace explicitamente E há
        # múltiplos namespaces DEILE no cluster, apresenta um menu de seleção
        # antes de abrir o painel.
        if context is None or (
            getattr(context, "namespace", _NS_DEFAULT) == _NS_DEFAULT
            and not getattr(context, "k8s_force", False)
            and not getattr(context, "local_force", False)
        ):
            available_ns = discover_deile_namespaces()
            if len(available_ns) > 1:
                # Prompt simples sem Rich (terminal pode não suportar fancy UI
                # antes do Live iniciar) — mostra a lista e pede número.
                print("\nVários namespaces DEILE detectados no cluster:")
                for idx, ns in enumerate(available_ns, 1):
                    print(f"  [{idx}] {ns}")
                selected_ns: Optional[str] = None
                while selected_ns is None:
                    try:
                        raw = input(
                            f"Escolha o namespace [1-{len(available_ns)}] "
                            f"(Enter = {available_ns[0]}): "
                        ).strip()
                        if raw == "":
                            selected_ns = available_ns[0]
                        elif raw.isdigit() and 1 <= int(raw) <= len(available_ns):
                            selected_ns = available_ns[int(raw) - 1]
                        else:
                            print("  Número inválido — tente novamente.")
                    except (EOFError, KeyboardInterrupt):
                        # Não-interativo ou Ctrl-C: usa o primeiro NS.
                        selected_ns = available_ns[0]
                        print(f"\nUsando namespace: {selected_ns}")
                if context is None:
                    context = RuntimeContext.detect(namespace=selected_ns)
                elif hasattr(context, "__class__"):
                    # RuntimeContext é frozen; cria novo com namespace correto.
                    from dataclasses import \
                        replace as _dc_replace  # noqa: PLC0415
                    context = _dc_replace(context, namespace=selected_ns)

        ctx = context if context is not None else RuntimeContext.detect()
        try:
            data = PanelData.from_context(ctx)
            # Toque inicial pra detectar fontes mortas cedo sem custo
            # perceptível (cada provider cai em fallback se falhar).
            data.pods.get()
        except Exception:  # noqa: BLE001
            logger.warning(
                "falha bootstrap providers, caindo em modo demo",
                exc_info=True,
            )
            data = None

    app = PanelApp(_build_views(data), data=data, memdebug=memdebug)
    try:
        return app.run()
    except KeyboardInterrupt:
        return 130
