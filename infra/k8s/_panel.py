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
from typing import Any, Dict, List, Optional, Tuple

from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


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


# ===== mock data (Fase 1) ===================================================
#
# Substituído pela camada `_panel_data.py` na Fase 2. Mantido aqui para o
# esqueleto rodar standalone sem cluster.

@dataclass
class _PodRow:
    icon: str
    name: str
    status: str
    age: str
    restarts: str
    last_activity: str
    doing_now: str
    busy: bool = False


_MOCK_PODS: List[_PodRow] = [
    _PodRow("●", "deile-pipeline-7f8d", "Running", "17m", "0",
            "23s ago", "tick #287, idle"),
    _PodRow("●", "deile-worker-abc-1", "Running", "2h", "0",
            "4m ago", "idle"),
    _PodRow("⚡", "deile-worker-abc-2", "Running", "2h", "0",
            "0s ago", "IMPL #296 [t+240s]", busy=True),
    _PodRow("●", "deilebot-xyz", "Running", "5h", "0",
            "1m ago", "0 inflight DMs"),
    _PodRow("●", "deile-shell-def0", "Running", "5h", "0",
            "—", "idle"),
]

_MOCK_PIPELINE = {
    "tick_n": 287,
    "tick_age_s": 23,
    "issues_open": 12,
    "issue_states": {"nova": 2, "em_refinamento": 1, "revisada": 0,
                     "em_impl": 3, "bloqueada": 1, "outros": 5},
    "prs_open": 4,
    "pr_states": {"pendente": 1, "em_andamento": 1, "concluida": 0,
                  "outros": 2},
    "last_decisions": [
        ("#296", "claim+dispatch implement"),
        ("#294", "refine round 2 (em_refinamento)"),
        ("#281", "blocked (escalou TIMEOUT 2×)"),
    ],
}

_MOCK_ACTIVITY: List[Tuple[str, str, str, str, str]] = [
    ("14:51:48", "worker-2", "start  implement", "#296", "attempt 2  budget 0s"),
    ("14:51:30", "pipeline", "claim", "#294", "batch:7a2c → analyst"),
    ("14:50:55", "worker-1", "done   review", "#293", "veredito APROVADO"),
    ("14:50:30", "pipeline", "merge", "PR#293", "commit 6bba656 (green suite)"),
    ("14:49:12", "notifier", "→discord", "", "PR #293 merged"),
    ("14:48:30", "pipeline", "resume", "#281", "attempt 3/5  backoff 4×"),
    ("14:47:30", "pipeline", "classify", "PR#293", "→ ~review:pendente"),
    ("14:46:08", "worker-1", "start  review", "PR#293", "budget 0s"),
]

_MOCK_ALERTS: List[Tuple[str, str]] = [
    ("⚠", "#281 attempt 3/5 — próximo loop com mesmo erro vai bloquear"),
]

_MOCK_TOKENS = {
    "providers": [("anthropic", 9.43), ("openai", 1.12), ("deepseek", 0.85)],
    "total": 11.40,
}


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
    """Tela-mãe: pods + pipeline + activity + alerts + tokens."""

    name = "dashboard"
    title = "Dashboard"
    refresh_s = 3.0

    HOTKEYS = (
        "[1]Pod watch  [2]Pipeline  [3]Issues/PRs  [4]Logs split  "
        "[5]Tokens  [a]ctions  [m]odel  [?]help  [q]uit"
    )

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
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
        tbl.add_column(" ", width=2, no_wrap=True)
        tbl.add_column("pod", style="bold")
        tbl.add_column("status", width=8)
        tbl.add_column("age", width=5)
        tbl.add_column("r", width=2, justify="right")
        tbl.add_column("last-activity", width=12)
        tbl.add_column("doing now")
        for p in _MOCK_PODS:
            icon_style = "bold yellow" if p.busy else "green"
            doing_style = "bold yellow" if p.busy else "dim"
            tbl.add_row(
                Text(p.icon, style=icon_style),
                p.name,
                Text(p.status, style="green"),
                p.age,
                p.restarts,
                Text(p.last_activity, style="dim"),
                Text(p.doing_now, style=doing_style),
            )
        return Panel(tbl, title="[bold]PODS[/bold]",
                     title_align="left", border_style="cyan")

    def _pipeline_panel(self) -> Panel:
        states = _MOCK_PIPELINE["issue_states"]
        pr_states = _MOCK_PIPELINE["pr_states"]
        lines = [
            Text.assemble(
                ("tick #", "dim"),
                (str(_MOCK_PIPELINE["tick_n"]), "bold cyan"),
                ("  ·  ", "dim"),
                (f"{_MOCK_PIPELINE['tick_age_s']}s ago", "dim"),
            ),
            Text.assemble(
                (f"Issues abertas: {_MOCK_PIPELINE['issues_open']}",
                 "bold"),
                ("  ", ""),
                (f"nova:{states['nova']}  refine:{states['em_refinamento']}  "
                 f"revisada:{states['revisada']}  "
                 f"impl:{states['em_impl']}  block:{states['bloqueada']}",
                 "dim"),
            ),
            Text.assemble(
                (f"PRs abertos:    {_MOCK_PIPELINE['prs_open']}", "bold"),
                ("  ", ""),
                (f"pendente:{pr_states['pendente']}  "
                 f"em_andamento:{pr_states['em_andamento']}  "
                 f"concluida:{pr_states['concluida']}", "dim"),
            ),
        ]
        return Panel(Group(*lines), title="[bold]PIPELINE[/bold]",
                     title_align="left", border_style="magenta")

    def _activity_panel(self) -> Panel:
        tbl = Table(box=box.SIMPLE, expand=True, show_header=False,
                    pad_edge=False)
        tbl.add_column(width=8, style="dim")
        tbl.add_column(width=10, style="bold cyan")
        tbl.add_column(width=18)
        tbl.add_column(width=8, style="yellow")
        tbl.add_column()
        for ts, actor, action, target, detail in _MOCK_ACTIVITY:
            tbl.add_row(ts, actor, action, target, Text(detail, style="dim"))
        return Panel(tbl, title="[bold]ACTIVITY[/bold] (últimos 10)",
                     title_align="left", border_style="green")

    def _alerts_panel(self) -> Panel:
        if not _MOCK_ALERTS:
            body: RenderableType = Text("· sem alertas críticos", style="dim")
        else:
            lines = [
                Text.assemble((f"{icon} ", "bold yellow"), msg)
                for icon, msg in _MOCK_ALERTS
            ]
            body = Group(*lines)
        return Panel(body, title="[bold]ALERTS[/bold]",
                     title_align="left", border_style="yellow")

    def _tokens_panel(self) -> Panel:
        bits: List[Text] = []
        for prov, cost in _MOCK_TOKENS["providers"]:
            bits.append(Text.assemble(
                (f"{prov} ", "dim"),
                (f"${cost:.2f}", "bold green"),
            ))
        line = Text("   ").join(bits)
        total = Text.assemble(
            ("total 24h: ", "dim"),
            (f"${_MOCK_TOKENS['total']:.2f}", "bold green"),
        )
        return Panel(Group(line, total), title="[bold]TOKENS (24h)[/bold]",
                     title_align="left", border_style="green")

    def _decisions_panel(self) -> Panel:
        lines = [
            Text.assemble(
                (f"{ref}  ", "bold cyan"),
                (desc, "dim"),
            )
            for ref, desc in _MOCK_PIPELINE["last_decisions"]
        ]
        return Panel(Group(*lines),
                     title="[bold]ÚLTIMAS DECISÕES[/bold]",
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

    def __init__(self, views: Dict[str, View], root: str = "dashboard"):
        self.views = views
        self.stack: List[View] = [views[root]]
        self.running = True
        self.console = Console()
        self.settings = _Settings()
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

def _build_views() -> Dict[str, View]:
    """Registry das views. Próximas fases trocam os stubs por views reais."""
    return {
        "dashboard": DashboardView(),
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
    """Entry point chamado pelo `deploy.py panel`."""
    app = PanelApp(_build_views())
    try:
        return app.run()
    except KeyboardInterrupt:
        return 130
