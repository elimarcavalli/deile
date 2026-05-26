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
from _panel_data import BackgroundRefresher, PanelData  # noqa: F401
from _panel_data import \
    _audit_security_policy_change as pd_audit_security_policy_change
from _panel_data import _fmt_age, kubectl_bin  # noqa: F401
from _panel_data import set_preferred_model as pd_set_preferred_model
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


def _pod_rows(data: Optional[PanelData]) -> List[PodRow]:
    """Converte o estado dos providers em linhas da tabela de pods."""
    if data is None:
        return [
            PodRow(p.icon, p.name, p.role, p.status, p.age, p.restarts,
                   p.last_activity, p.doing_now, p.busy)
            for p in demo.PODS
        ]
    workers = data.workers.get()
    ps = data.pipeline.get()
    rows: List[PodRow] = []
    for p in data.pods.get():
        # Last-activity humano por role.
        if p.role == "worker":
            ws = workers.get(p.name)
            last = _fmt_age(ws.last_activity_s) + " ago" if ws else "—"
            doing = ws.last_substantive_body[:32] if ws and ws.last_substantive_body \
                else ("ocupado" if ws and ws.busy else "idle")
            busy = bool(ws and ws.busy)
            icon = "⚡" if busy else "●"
        elif p.role == "pipeline":
            last = (_fmt_age(ps.last_action_age_s) + " ago"
                    if ps.last_action_age_s is not None else "—")
            doing = ps.last_action_summary[:48] or "idle"
            busy = (ps.last_action_age_s is not None
                    and ps.last_action_age_s < 60)
            icon = "⚡" if busy else "●"
        else:
            last = "—"
            doing = "—"
            busy = False
            icon = "●"

        ready_label = p.status
        if p.status == "Running" and not p.ready:
            ready_label = "NotReady"

        rows.append(PodRow(
            icon=icon, name=p.name, role=p.role,
            status=ready_label, age=_fmt_age(p.age_s),
            restarts=str(p.restarts), last_activity=last,
            doing_now=doing, busy=busy,
        ))
    return rows


# ===== renderers reaproveitáveis ============================================
#
# Mantidos no nível de módulo para que as views da Fase 3+ reusem o mesmo
# estilo visual sem duplicar código.

def _head_panel(view_title: str, app: "PanelApp") -> Panel:
    """Cabeçalho dinâmico: modo (k8s/local/híbrido) + namespace + clock + cadência."""
    paused = " ⏸ pausado" if app.paused else ""
    speed = "" if app.refresh_mult == 1.0 else f" ×{app.refresh_mult:g}"
    clock = time.strftime("%Y-%m-%d %H:%M:%S")
    head = Text()
    head.append("DEILE Stack", style="bold cyan")
    head.append("  ·  ")
    head.append(view_title, style="bold")
    head.append("  ·  ")
    head.append(clock, style="dim")
    head.append(f"  ·  refresh {app.current_refresh_s:.1f}s{speed}{paused}",
                style="dim yellow" if app.paused else "dim")
    # Linha 2: contexto efetivo. Usa o RuntimeContext quando há `data`; em
    # modo demo cai no rótulo fixo histórico.
    if app.data is not None:
        ctx = app.data.context
        mode_style = ("bold green" if ctx.mode_label.startswith("k8s + local")
                      else "bold cyan" if "k8s" in ctx.mode_label
                      else "bold yellow" if "local" in ctx.mode_label
                      else "bold red")
        sub = Text.assemble(
            ("mode: ", "dim"), (ctx.mode_label, mode_style),
            ("   cluster: ", "dim"), (ctx.cluster_label, "dim"),
            ("   namespace: ", "dim"), (ctx.namespace, "bold"),
            ("   repo: ", "dim"), (ctx.repo or "—", "dim"),
        )
    else:
        sub = Text("mode: demo (mocks)   cluster: —   namespace: —",
                   style="dim yellow")
    pieces: List[RenderableType] = [head, sub]
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


def _footer_panel(hotkeys: str) -> Panel:
    """Rodapé com a linha de hotkeys da view ativa."""
    return Panel(Text(hotkeys, style="dim"), border_style="dim", box=box.SIMPLE)


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

    HOTKEYS = (
        "[1]Pod watch  [2]Pipeline  [3]Issues/PRs  [4]Logs split  "
        "[5]Tokens  [n]otifier  [a]ctions  [m]odel  [?]help  [q]uit"
    )

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data

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
        children.extend([
            Layout(name="middle", size=8),
            Layout(self._activity_panel(), name="activity"),
            Layout(name="bottom", size=5),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
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
        rows = _pod_rows(self.data)
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
        }
        if key in nav:
            return ActionResult.nav(nav[key])
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
        from _panel_data import (delete_pod,  # noqa: PLC0415
                                  kill_local_pid,
                                  rollout_restart_all,
                                  rollout_restart_deployment)

        rows = self._rows()
        if action == "R":
            results = rollout_restart_all()
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
                ok, msg = delete_pod(row.name)
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
            ok, msg = rollout_restart_deployment(dep)
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
               "[c] clear log   [.] abrir log   [esc] volta   [q] sai")

    # Quantas linhas do buffer dumpamos no tempfile quando o usuário pede
    # "abrir log" num pod k8s. Suficiente pra contexto, sem inflar o disco.
    _DUMP_TAIL_LINES = 2000

    # Janela (s) em que a mensagem de status fica visível no header do log.
    _STATUS_TTL_S = 6.0

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
              else "deile")
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
        wstate = self.data.workers.get().get(self.pod_name) \
            if self.pod_role == "worker" else None
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
        if wstate is not None:
            lines.append(Text.assemble(
                ("worker: ", "dim"),
                ("BUSY" if wstate.busy else "idle",
                 "bold yellow" if wstate.busy else "dim"),
                ("   last activity: ", "dim"),
                (_fmt_age(wstate.last_activity_s) + " ago"
                 if wstate.last_activity_s is not None else "—", "bold"),
            ))
        return Group(*lines)

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
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(self._header_body(),
                         title="[bold]POD[/bold]", title_align="left",
                         border_style="cyan"),
                   name="info", size=6),
            Layout(self._log_panel(), name="log"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
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
        return ActionResult()

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
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(name="middle", size=10),
            Layout(Panel(events_body,
                         title="[bold]EVENTS (mais recentes em cima)[/bold]",
                         title_align="left", border_style="green"),
                   name="events"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
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
    """Tabela de issues e PRs com labels cruzadas + filtros."""

    name = "issues-prs"
    title = "Issues & PRs"
    refresh_s = 1.0

    HOTKEYS = ("[a] all   [i] só issues   [p] só PRs   [b] só bloqueadas   "
               "[m] minhas   [↑/↓] navega   [enter] abrir URL   [esc] volta")

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.filter: str = "all"
        self.cursor: int = 0
        # Sem nome hardcoded: tenta env GH_USER → `gh api user` → vazio.
        self.my_login: str = self._resolve_login()

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
        issues, prs = snap.issues, snap.prs
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
        return issues, prs

    def _flat(self):
        issues, prs = self._rows()
        return list(issues) + list(prs)

    def _build_table(self, items, label: str) -> Panel:
        if not items:
            return Panel(Text(f"· nada em {label}", style="dim"),
                         title=f"[bold]{label.upper()}[/bold]",
                         title_align="left", border_style="dim")
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        tbl.add_column(" ", width=2)
        tbl.add_column("#", width=6)
        tbl.add_column("workflow", width=22)
        tbl.add_column("review", width=14)
        tbl.add_column("updated", width=10)
        tbl.add_column("assignees", width=18)
        tbl.add_column("title")
        now = datetime.now(timezone.utc)
        flat = self._flat()
        for it in items:
            global_idx = flat.index(it)
            marker = "▶" if global_idx == self.cursor else " "
            wf_style = "bold red" if it.blocked else "cyan"
            age_s = ((now - it.updated_at).total_seconds()
                     if it.updated_at else None)
            tbl.add_row(
                Text(marker, style="bold cyan"),
                str(it.number),
                Text(it.workflow or "—", style=wf_style),
                Text(it.review or "—", style="magenta" if it.review else "dim"),
                _fmt_age(age_s),
                ", ".join(it.assignees) or "—",
                Text(it.title[:60], style="dim"),
            )
        return Panel(tbl, title=f"[bold]{label.upper()}[/bold]",
                     title_align="left", border_style="cyan")

    def render(self, app: "PanelApp") -> RenderableType:
        issues, prs = self._rows()
        flat = list(issues) + list(prs)
        if flat:
            self.cursor = max(0, min(self.cursor, len(flat) - 1))
        filter_label = {
            "all": "todos", "i": "só issues", "p": "só PRs",
            "b": "só bloqueadas", "m": f"minhas (@{self.my_login})",
        }[self.filter]
        filter_panel = Panel(
            Text.assemble(
                ("filtro: ", "dim"), (filter_label, "bold yellow"),
                ("    issues: ", "dim"), (str(len(issues)), "bold"),
                ("    PRs: ", "dim"), (str(len(prs)), "bold"),
            ),
            border_style="dim",
        )
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(filter_panel, name="filter", size=3),
            Layout(self._build_table(issues, "Issues"), name="issues"),
            Layout(self._build_table(prs, "PRs"), name="prs"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        flat = self._flat()
        if key in ("a", "i", "p", "b", "m"):
            self.filter = key
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
                _ActionSpec("status",             [py, deploy, "k8s", "status"]),
                _ActionSpec("restart",            [py, deploy, "k8s", "restart", "--yes"], mutates=True),
                _ActionSpec("build (no restart)", [py, deploy, "k8s", "build", "--yes"], mutates=True),
                _ActionSpec("build + restart",    [py, deploy, "k8s", "build", "--restart", "--yes"], mutates=True),
                _ActionSpec("up (provisiona)",    [py, deploy, "k8s", "up", "--yes"], mutates=True),
                _ActionSpec("stop (scale 0)",     [py, deploy, "k8s", "stop", "--yes"], mutates=True),
                _ActionSpec("start (scale 1)",    [py, deploy, "k8s", "start", "--yes"], mutates=True),
                _ActionSpec("test (Job one-shot)",[py, deploy, "k8s", "test", "--yes"], mutates=True),
                _ActionSpec("DOWN (apaga ns)",    [py, deploy, "k8s", "down", "--yes"], destructive=True, mutates=True),
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
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(menu, title="[bold]AÇÕES[/bold]",
                         title_align="left", border_style="cyan"),
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

        ns = self.data.context.namespace if self.data.context else "deile"
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
            ("1-5, a, m, n",  "drill em sub-view (no dashboard)"),
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
                    if self._handle_global(key):
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

    if force_demo:
        data: Optional[PanelData] = None
    else:
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
