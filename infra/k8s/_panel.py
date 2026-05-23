"""TUI ao vivo de monitoramento da stack DEILE no Kubernetes.

Painel full-screen com `rich.Live` que cruza o estado do cluster (pods,
deployments, métricas) com o estado da fonte de verdade do pipeline
(issues + PRs no GitHub) e do trabalho LLM (logs, progress.md, custos).

Esqueleto (Fase 1): layout completo do dashboard + key handler navegável
+ stub das sub-views. Dados ainda são mock — as Fases 2+ ligam providers
reais (kubectl, gh, sqlite de usage).

Uso:
    python3 infra/k8s/deploy.py panel        # entra no painel
    [1-5] drill em sub-view   [esc] back   [q] quit
    [p] pause refresh         [+/-] velocidade   [r] force refresh
    [s] snapshot              [?] ajuda
"""

from __future__ import annotations

import os
import select
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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

from _panel_data import PanelData, _fmt_age  # noqa: F401 (re-export)
import _panel_demo as demo


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
        "[5]Tokens  [a]ctions  [m]odel  [?]help  [q]uit"
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
            "1": "pod-watch",
            "2": "pipeline-timeline",
            "3": "issues-prs",
            "4": "logs-split",
            "5": "tokens",
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
        "pod-watch": StubView(
            "pod-watch", "Pod Watch", "Fase 4",
            "Drill-in num pod (worker/pipeline/bot): "
            "phase atual, progress.md, log live, recent tasks.",
        ),
        "pipeline-timeline": StubView(
            "pipeline-timeline", "Pipeline Timeline", "Fase 5",
            "Últimos N ticks com duração + actions. "
            "Stats (P95/P99) e histograma 24h.",
        ),
        "issues-prs": StubView(
            "issues-prs", "Issues & PRs", "Fase 5",
            "Tabela cruzada cluster ↔ GitHub: estado, tempo no estado, "
            "assignee, próxima ação esperada.",
        ),
        "logs-split": StubView(
            "logs-split", "Logs Split", "Fase 4",
            "Pipeline + Worker-1 + Worker-2 lado a lado, follow real.",
        ),
        "tokens": StubView(
            "tokens", "Tokens & Custos", "Fase 6",
            "Gasto 24h por provider e por task type. "
            "Top 5 issues mais caras.",
        ),
        "actions": StubView(
            "actions", "Ações", "Fase 6",
            "Build / restart / up / down / test acionáveis sem sair "
            "do painel.",
        ),
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
