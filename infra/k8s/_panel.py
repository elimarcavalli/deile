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

import os
import select
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from _panel_data import PanelData, _fmt_age, kubectl_bin  # noqa: F401
import _panel_demo as demo
import collections
import re
import shutil
import subprocess
import threading
from pathlib import Path

_HEALTH_LINE_RE = re.compile(r"GET /v1/health")


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

        Decodifica ESC sozinho (vs prefix de seta) com timeout de 50ms — o
        suficiente para distinguir num terminal local sem deixar o usuário
        sentir lag.
        """

        def __init__(self):
            self._fd: Optional[int] = (
                sys.stdin.fileno() if sys.stdin.isatty() else None
            )
            self._old = None

        def __enter__(self):
            if self._fd is not None:
                self._old = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
            return self

        def __exit__(self, *exc):
            if self._fd is not None and self._old is not None:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

        def read(self, timeout: float = 0.0) -> Optional[str]:
            if self._fd is None:
                return None
            if not select.select([sys.stdin], [], [], timeout)[0]:
                return None
            ch = sys.stdin.read(1)
            if ch != "\x1b":
                return ch
            # ESC sozinho ou prefix CSI?
            if not select.select([sys.stdin], [], [], 0.05)[0]:
                return "ESC"
            seq = sys.stdin.read(1)
            if seq != "[":
                return "ESC"
            buf: List[str] = []
            while select.select([sys.stdin], [], [], 0.05)[0]:
                c = sys.stdin.read(1)
                buf.append(c)
                if c.isalpha() or c == "~":
                    break
            code = "".join(buf)
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
    rows: List[ActivityRow] = []
    ps = data.pipeline.get()
    for ev in reversed(ps.events[-limit:]):
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
    """Cabeçalho com cluster + namespace + image + relógio + cadência."""
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
    sub = Text(
        "cluster: rancher-desktop (k3s)   namespace: deile   "
        "image: deile-stack:local",
        style="dim",
    )
    return Panel(Group(head, sub), border_style="cyan", box=box.HEAVY)


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
    refresh_s = 3.0

    HOTKEYS = (
        "[1]Pod watch  [2]Pipeline  [3]Issues/PRs  [4]Logs split  "
        "[5]Tokens  [n]otifier  [a]ctions  [m]odel  [?]help  [q]uit"
    )

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data

    def render(self, app: "PanelApp") -> RenderableType:
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(self._pods_panel(), name="pods", size=10),
            Layout(name="middle", size=8),
            Layout(self._activity_panel(), name="activity"),
            Layout(name="bottom", size=5),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
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
    # Aliases pra encurtar nomes longos no header.
    short = {"em_refinamento": "refine", "em_arquitetura": "arq",
             "em_implementacao": "impl", "em_pr": "pr",
             "aguardando_stakeholder": "aguard"}
    bits = []
    for k, v in counts.items():
        if v == 0:
            continue
        bits.append(f"{short.get(k, k)}:{v}")
    return "  ".join(bits) if bits else "—"


class _LogStreamer:
    """Background `kubectl logs -f` que enche uma deque rolling.

    Pensado para ser dono curto-prazo: criado pelo `PodWatchView.on_mount`
    e parado no `on_unmount`. O processo `kubectl` recebe SIGTERM; após
    2s sem morrer, SIGKILL. A thread de leitura é daemon — segura contra
    vazamento se o app fechar antes do `stop`.
    """

    def __init__(self, kubectl: str, ns: str, pod: str,
                 tail: int = 50, maxlen: int = 300):
        self._cmd = [kubectl, "-n", ns, "logs", pod, "-f",
                     f"--tail={tail}", "--timestamps"]
        self.buf: "collections.deque[str]" = collections.deque(maxlen=maxlen)
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
            if self._stop.is_set():
                break
            self.buf.append(line.rstrip())

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None
        self._thread = None

    def snapshot(self, n: int = 30) -> List[str]:
        return list(self.buf)[-n:]


class PodPickerView(View):
    """Lista pods selecionável com setas / Enter abre o PodWatch."""

    name = "pod-picker"
    title = "Selecionar pod"
    refresh_s = 3.0

    HOTKEYS = "[↑/↓] navega   [enter] entra   [esc] volta   [q] sai"

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.cursor = 0

    def _rows(self) -> List[PodRow]:
        return _pod_rows(self.data)

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
        layout = Layout()
        layout.split_column(
            Layout(_head_panel(self.title, app), name="head", size=4),
            Layout(Panel(tbl, title="[bold]escolha um pod para assistir[/bold]",
                         title_align="left", border_style="cyan"),
                   name="body"),
            Layout(_footer_panel(self.HOTKEYS), name="footer", size=3),
        )
        return layout

    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        rows = self._rows()
        n = len(rows)
        if n == 0:
            return ActionResult()
        if key in ("UP", "k"):
            self.cursor = (self.cursor - 1) % n
            return ActionResult.refresh()
        if key in ("DOWN", "j"):
            self.cursor = (self.cursor + 1) % n
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            pod = rows[self.cursor]
            return ActionResult.nav("pod-watch", pod_name=pod.name,
                                    pod_role=pod.role)
        return ActionResult()


class PodWatchView(View):
    """Drill-in num pod: header + log live via kubectl logs -f.

    Fase 4 entrega o log streaming e o cabeçalho com o estado atual do
    pod (via PodsProvider) + busy/idle do worker (via WorkerProvider).
    Resources (cpu/mem via metrics-server) e o `.deile-progress.md`
    ficam para refinamento em fase posterior — exigem `kubectl top` /
    `kubectl exec` extras que valem encapsular como sub-providers.
    """

    name = "pod-watch"
    title = "Pod Watch"
    refresh_s = 1.0

    HOTKEYS = ("[f] follow on/off   [h] mostrar/esconder health   "
               "[c] clear log   [esc] volta   [q] sai")

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.pod_name: str = ""
        self.pod_role: str = ""
        self.streamer: Optional[_LogStreamer] = None
        self.following: bool = True
        # Health-checks lotam o buffer em workers ociosos; por default escondemos.
        self.hide_health: bool = True

    def on_mount(self, app: "PanelApp") -> None:
        # Payload da navegação vem em `app.last_payload` (setado pelo PanelApp).
        payload = getattr(app, "last_payload", {}) or {}
        self.pod_name = payload.get("pod_name", "")
        self.pod_role = payload.get("pod_role", "")
        kubectl = kubectl_bin()
        if not self.pod_name or kubectl is None:
            return
        self.streamer = _LogStreamer(kubectl, "deile", self.pod_name,
                                     tail=40, maxlen=400)
        self.streamer.start()

    def on_unmount(self, app: "PanelApp") -> None:
        if self.streamer is not None:
            self.streamer.stop()
            self.streamer = None

    def _header_body(self) -> RenderableType:
        if self.data is None:
            return Text("(modo demo — sem dados reais do pod)", style="dim yellow")
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
        title = (f"[bold]LIVE LOG[/bold]  ·  {follow_label}  ·  "
                 f"{health_label}{hidden_label}  ·  {self.pod_name}")
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
        return ActionResult()


class PipelineTimelineView(View):
    """Timeline de eventos do pipeline + stats + histograma 24h."""

    name = "pipeline-timeline"
    title = "Pipeline Timeline"
    refresh_s = 5.0

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
    refresh_s = 10.0

    HOTKEYS = ("[a] all   [i] só issues   [p] só PRs   [b] só bloqueadas   "
               "[m] minhas   [↑/↓] navega   [enter] abrir URL   [esc] volta")

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.filter: str = "all"
        self.cursor: int = 0
        self.my_login: str = os.environ.get("GH_USER", "elimarcavalli")

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

    Sem erro se nenhum bin disponível — é só um nice-to-have.
    """
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True, timeout=2)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


class TokensView(View):
    """Detalhe de custos via UsageRepository.

    Mostra breakdown por provider (1h / 24h), records e top 5 sessions.
    A view tem TTL alto (60s) — o SQLite é local mas re-abrir a cada
    poucos segundos é desperdício.
    """

    name = "tokens"
    title = "Tokens & Custos"
    refresh_s = 60.0

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

_AUDIT_LOG_RE = re.compile(r'\{"ts":\s*"[^"]+",\s*"level":\s*"\w+",'
                           r'\s*"logger":\s*"deilebot\.audit"')


class NotifierEchoView(View):
    """Últimas mensagens de I/O do bot (audit log)."""

    name = "notifier-echo"
    title = "Notifier Echo"
    refresh_s = 5.0

    HOTKEYS = "[r] força refresh   [esc] volta   [q] sai"

    BOT_DEPLOY = "deilebot"
    TAIL = 500

    def __init__(self, data: Optional[PanelData] = None):
        self.data = data

    def _fetch_lines(self) -> List[Dict[str, Any]]:
        kubectl = kubectl_bin()
        if kubectl is None:
            return []
        try:
            out = subprocess.run(
                [kubectl, "-n", "deile", "logs",
                 f"deploy/{self.BOT_DEPLOY}", f"--tail={self.TAIL}"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if out.returncode != 0:
            return []
        events: List[Dict[str, Any]] = []
        import json as _json
        for line in out.stdout.splitlines():
            if not _AUDIT_LOG_RE.search(line):
                continue
            try:
                ev = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            events.append(ev)
        return events

    def render(self, app: "PanelApp") -> RenderableType:
        events = self._fetch_lines()
        if not events:
            body: RenderableType = Text(
                "Nenhum evento `deilebot.audit` recente.\n\n"
                "Quando o bot recebe ou envia mensagens, este painel mostra\n"
                "evento + payload (op, reason, channel, etc).",
                style="dim",
            )
        else:
            tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
            tbl.add_column("ts", width=20, style="dim")
            tbl.add_column("event", width=22, style="bold")
            tbl.add_column("status", width=10)
            tbl.add_column("detail")
            for ev in events[-20:]:
                ts = (ev.get("ts", "") or "")[-19:]
                name = ev.get("event") or ev.get("message", "")
                ok = "OK" if "sent" in name or "received" in name else \
                     "FAIL" if "failed" in name else "—"
                ok_style = "green" if ok == "OK" else \
                           "red" if ok == "FAIL" else "dim"
                payload = ev.get("payload") or {}
                detail = ", ".join(f"{k}={v}" for k, v in payload.items())
                tbl.add_row(ts, name, Text(ok, style=f"bold {ok_style}"),
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


class _ActionRunner:
    """Wrap de subprocess streaming pra ActionsView.

    Diferente do _LogStreamer (que vive enquanto a view existe), este é
    one-shot: roda o comando, encerra, deixa o output no buffer pra
    consulta. Cancelável com .stop().
    """

    def __init__(self, cmd: List[str], maxlen: int = 500):
        self.cmd = cmd
        self.buf: "collections.deque[str]" = collections.deque(maxlen=maxlen)
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
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
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

    @staticmethod
    def _actions() -> List[_ActionSpec]:
        # `python3 infra/k8s/deploy.py` resolvido em runtime — o painel vive
        # dentro do mesmo repo.
        repo_root = str(Path(__file__).resolve().parent.parent.parent)
        py = sys.executable
        deploy = f"{repo_root}/infra/k8s/deploy.py"
        # `--yes` pula confirmação interna do deploy.py — a confirmação aqui
        # é nossa, na view, antes de chamar.
        return [
            _ActionSpec("status",            [py, deploy, "k8s", "status"]),
            _ActionSpec("restart",           [py, deploy, "k8s", "restart", "--yes"]),
            _ActionSpec("build (no restart)",[py, deploy, "k8s", "build", "--yes"]),
            _ActionSpec("build + restart",   [py, deploy, "k8s", "build", "--restart", "--yes"]),
            _ActionSpec("up (provisiona)",   [py, deploy, "k8s", "up", "--yes"]),
            _ActionSpec("stop (scale 0)",    [py, deploy, "k8s", "stop", "--yes"], destructive=False),
            _ActionSpec("start (scale 1)",   [py, deploy, "k8s", "start", "--yes"]),
            _ActionSpec("test (Job one-shot)",[py, deploy, "k8s", "test", "--yes"]),
            _ActionSpec("DOWN (apaga ns)",   [py, deploy, "k8s", "down", "--yes"], destructive=True),
        ]

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
            confirm_body = Group(
                Text(f"Vai rodar a ação DESTRUTIVA: {self.confirm_for.label}",
                     style="bold red"),
                Text(" ".join(self.confirm_for.cmd), style="dim"),
                Text(),
                Text("Aperte [y] para confirmar, [n] para cancelar.",
                     style="bold yellow"),
            )
            confirm_panel: Optional[RenderableType] = Panel(
                confirm_body, title="[bold red]CONFIRMAÇÃO[/bold red]",
                title_align="left", border_style="red",
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
            if key == "n":
                self.confirm_for = None
                return ActionResult.refresh()
            return ActionResult()
        if key == "c" and self.runner is not None and self.runner.running:
            self.runner.stop()
            return ActionResult.refresh()
        if key.isdigit():
            idx = int(key) - 1
            actions = self._actions()
            if 0 <= idx < len(actions):
                spec = actions[idx]
                if spec.destructive:
                    self.confirm_for = spec
                else:
                    self._dispatch(spec)
                return ActionResult.refresh()
        return ActionResult()

    def _dispatch(self, spec: _ActionSpec) -> None:
        if self.runner is not None and self.runner.running:
            return                # já tem ação rodando, ignora
        self.runner = _ActionRunner(spec.cmd)
        self.last_action = spec.label
        self.runner.start()

    def on_unmount(self, app: "PanelApp") -> None:
        # Mata runner pendente quando o usuário sai da view (ESC).
        if self.runner is not None and self.runner.running:
            self.runner.stop()


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
            ("1-5, a, m",   "drill em sub-view (no dashboard)"),
            ("esc",         "volta à view anterior (ou ao dashboard)"),
            ("q",           "sai do painel"),
            ("p",           "pause / resume refresh automático"),
            ("+ / -",       "acelera / desacelera o refresh (×0.5 a ×4)"),
            ("r",           "força um refresh imediato"),
            ("s",           "snapshot: salva a tela atual em .deile/snapshots/"),
            ("?",           "esta tela"),
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


class PanelApp:
    """Loop principal: rich.Live + key handler + view stack.

    O stack é uma pilha de views; navegação empilha e ESC desempilha.
    Cada view declara `refresh_s` que combinada com `refresh_mult` define
    a cadência efetiva.
    """

    def __init__(self, views: Dict[str, View], root: str = "dashboard",
                 data: Optional[PanelData] = None):
        self.views = views
        self.stack: List[View] = [views[root]]
        self.running = True
        self.console = Console()
        self.settings = _Settings()
        self.data = data
        self.last_payload: Dict[str, Any] = {}
        self._last_render = 0.0

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

    # --- snapshots ---

    def _snapshot(self) -> Optional[str]:
        from pathlib import Path
        out_dir = Path(
            self.settings.snapshots_dir
            or os.path.expanduser("~/.deile/snapshots")
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"panel-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        capture = Console(record=True, width=self.console.size.width)
        capture.print(self.current_view.render(self))
        path.write_text(capture.export_text(), encoding="utf-8")
        return str(path)

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
            self._last_render = 0.0       # força próximo tick a renderizar
            if self.data is not None:
                self.data.force_refresh_all()
            return True
        if key == "s":
            path = self._snapshot()
            if path:
                # NOTE: não printa aqui (quebraria a TUI); a próxima
                # render mostra no head. Fase 3 vai pôr um toast.
                self.settings.snapshots_dir = self.settings.snapshots_dir
            return True
        return False

    # --- main loop ---

    def run(self) -> int:
        if not sys.stdin.isatty():
            self.console.print(
                "[yellow]painel exige terminal interativo "
                "(sem TTY).[/yellow]"
            )
            return 1
        with KeyReader() as keys, Live(
            self.current_view.render(self),
            console=self.console,
            screen=True,
            refresh_per_second=10,
            transient=False,
        ) as live:
            self._last_render = time.monotonic()
            while self.running:
                key = keys.read(timeout=0.1)
                if key:
                    if not self._handle_global(key):
                        result = self.current_view.handle_key(key, self)
                        self._apply(result)
                if not self.running:
                    break
                if (not self.paused) and (
                    time.monotonic() - self._last_render
                    >= self.current_refresh_s
                ):
                    live.update(self.current_view.render(self))
                    self._last_render = time.monotonic()
                elif self._last_render == 0.0:
                    # Force-refresh pedido via [r].
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
        "model-switcher": StubView(
            "model-switcher", "Trocar modelo", "Fase 9",
            "Lista modelos disponíveis e modelo em uso por pod. "
            "Troca em runtime sem rebuild.",
        ),
    }


def run_panel() -> int:
    """Entry point chamado pelo `deploy.py panel`.

    Tenta levantar os providers reais (kubectl/gh/SQLite). Se `kubectl`
    não existe, cai em modo demo (mocks) com aviso — UI ainda abre.
    """
    try:
        data: Optional[PanelData] = PanelData.default()
        # Toque inicial pra detectar fonte morta cedo (sem custo perceptível —
        # o provider mais pesado é o gh, mas cai em fallback se falhar).
        data.pods.get()
    except Exception:
        data = None

    app = PanelApp(_build_views(data), data=data)
    try:
        return app.run()
    except KeyboardInterrupt:
        return 130
