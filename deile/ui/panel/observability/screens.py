"""Rich-based renderers for the 3 observability screens (issue #347).

Every screen is a pure function over a state dict — no async, no network,
no Rich Live region.  The panel main loop (:mod:`panel_main`) fetches state
asynchronously, then passes it here to render, then prints the result via
``console.print``.  Tests can verify the rendered text with
``rich.console.Console.capture()`` without needing the network or asyncio.

Adapts to terminal width per pillar 03 §15: every ``Table``/``Panel`` is
constructed *without* a hardcoded ``width`` so Rich computes the layout
from ``console.width`` on each render.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fmt_ts(value: Optional[float]) -> str:
    """Format a unix-epoch float (or None) as a local ``HH:MM:SS`` string.

    Returns ``"--:--:--"`` when value is missing/invalid so the layout stays
    aligned (callers don't need to handle ``None`` separately).
    """
    if value is None:
        return "--:--:--"
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "--:--:--"


def _fmt_duration(seconds: Optional[float]) -> str:
    """Format seconds as a compact ``HhMmSs`` string (max two non-zero units)."""
    if seconds is None or seconds < 0:
        return "--"
    s = int(seconds)
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _fmt_cost(value: Optional[float]) -> str:
    if value is None:
        return "$0.00"
    return f"${value:.2f}"


def _truncate(s: Any, max_len: int) -> str:
    text = str(s) if s is not None else ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


# --------------------------------------------------------------------------- #
# Cluster Status (screen 1)
# --------------------------------------------------------------------------- #


@dataclass
class ClusterStatusData:
    """Read-only struct the renderer needs.

    The panel main loop builds this from the various endpoints; tests
    construct it directly.
    """

    pipeline_status: Dict[str, Any]
    backlog: List[Dict[str, Any]]
    recent_events: List[Dict[str, Any]]
    ledger: Dict[str, Dict[str, Any]]
    api_errors: List[str]


class ClusterStatusScreen:
    """Renderer for *Cluster Status* — default tab when the panel opens."""

    def render(self, data: ClusterStatusData) -> RenderableType:
        body = Table.grid(padding=(0, 1))
        body.add_column()
        body.add_row(self._render_summary(data.pipeline_status))
        body.add_row(self._render_backlog(data.backlog))
        body.add_row(self._render_recent(data.recent_events))
        body.add_row(self._render_ledger(data.ledger))
        if data.api_errors:
            errs = Text(" • ".join(data.api_errors), style="yellow")
            body.add_row(Panel(errs, title="API errors", border_style="yellow"))
        return Panel(
            body,
            title=f"DEILE CLUSTER  •  now {_fmt_ts(data.pipeline_status.get('now'))}",
            border_style="cyan",
        )

    @staticmethod
    def _render_summary(status: Dict[str, Any]) -> RenderableType:
        if not status:
            return Text("pipeline status unavailable", style="dim")
        last = _fmt_ts(status.get("last_tick_at"))
        nxt = _fmt_ts(status.get("next_tick_at"))
        ticks = status.get("ticks_total", 0)
        errors = status.get("errors_total", 0)
        uptime = _fmt_duration(status.get("uptime_seconds"))
        table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
        table.add_column(justify="left")
        table.add_column(justify="right")
        table.add_row(f"pipeline uptime {uptime}",
                      f"ticks={ticks} errors={errors}")
        table.add_row(f"last-tick: {last}", f"next-tick: {nxt}")
        pods = status.get("pods_seen") or {}
        if pods:
            pods_text = "  ".join(
                f"{name}={info.get('ready_replicas','?')}" for name, info in pods.items()
            )
            table.add_row(Text(pods_text, style="dim"), Text(""))
        return table

    @staticmethod
    def _render_backlog(items: List[Dict[str, Any]]) -> RenderableType:
        if not items:
            return Panel(
                Text("(empty — no eligible work for next tick)", style="dim"),
                title="Backlog", border_style="dim",
            )
        table = Table(expand=True, padding=(0, 1))
        table.add_column("Kind", style="cyan")
        table.add_column("#", justify="right", style="white")
        table.add_column("Title")
        table.add_column("Age", justify="right", style="dim")
        table.add_column("Why")
        for it in items[:10]:
            table.add_row(
                _truncate(it.get("kind"), 6),
                str(it.get("number") or ""),
                _truncate(it.get("title") or "", 40),
                _fmt_duration(it.get("age_seconds")),
                _truncate(it.get("why_eligible") or "", 40),
            )
        return Panel(table, title="Backlog", border_style="cyan")

    @staticmethod
    def _render_recent(events: List[Dict[str, Any]]) -> RenderableType:
        if not events:
            return Panel(
                Text("(no recent events)", style="dim"),
                title="Recent activity", border_style="dim",
            )
        table = Table(expand=True, padding=(0, 1), show_header=False)
        table.add_column("when", style="dim", justify="right")
        table.add_column("event", style="cyan")
        table.add_column("summary")
        for ev in events[:12]:
            table.add_row(
                _fmt_ts(ev.get("ts")),
                _truncate(ev.get("event_type"), 14),
                _truncate(ev.get("summary"), 70),
            )
        return Panel(table, title="Recent activity", border_style="cyan")

    @staticmethod
    def _render_ledger(ledger: Dict[str, Dict[str, Any]]) -> RenderableType:
        if not ledger:
            return Panel(
                Text("(empty — no in-flight dispatches)", style="dim"),
                title="Pipeline ledger", border_style="dim",
            )
        table = Table(expand=True, padding=(0, 1))
        table.add_column("Key", style="cyan")
        table.add_column("Stage")
        table.add_column("Task")
        table.add_column("Attempt", justify="right")
        for key, row in ledger.items():
            table.add_row(
                _truncate(key, 18),
                _truncate(row.get("stage"), 12),
                _truncate(row.get("task_id"), 16),
                str(row.get("attempt") or 1),
            )
        return Panel(table, title="Pipeline ledger", border_style="cyan")


# --------------------------------------------------------------------------- #
# Live Session Watch (screen 2)
# --------------------------------------------------------------------------- #


@dataclass
class LiveSessionData:
    """State for one live (or most-recent) claude-worker session.

    ``session`` is the summary row from ``/v1/sessions``; ``command`` and
    ``chat`` come from the per-task endpoints.  Any of them may be ``None``
    when the worker is down or the task is unknown.
    """

    session: Optional[Dict[str, Any]]
    command: Optional[Dict[str, Any]]
    chat: Optional[Dict[str, Any]]
    api_errors: List[str]


class LiveSessionScreen:
    """Renderer for *Live Session Watch*."""

    def render(self, data: LiveSessionData) -> RenderableType:
        if not data.session:
            return Panel(
                Text("(idle — no recent claude-worker session)", style="dim"),
                title="LIVE SESSION",
                border_style="dim",
            )
        s = data.session
        alive = bool(s.get("alive"))
        header = (
            f"TASK {_truncate(s.get('task_id'), 16)}  •  "
            f"{s.get('stage') or '-'}  •  "
            f"{s.get('branch') or '-'}  •  "
            f"{'●ALIVE' if alive else '○ENDED'}"
        )
        layout = Table.grid(padding=(0, 1))
        layout.add_column()
        layout.add_row(self._render_command(data.command))
        layout.add_row(self._render_metrics(s))
        layout.add_row(self._render_chat(data.chat))
        if data.api_errors:
            layout.add_row(
                Panel(Text(" • ".join(data.api_errors), style="yellow"),
                      title="API errors", border_style="yellow"),
            )
        return Panel(layout, title=header,
                     border_style="green" if alive else "dim")

    @staticmethod
    def _render_command(command: Optional[Dict[str, Any]]) -> RenderableType:
        if not command:
            return Text("(command unknown)", style="dim")
        cmd = command.get("cmd") or []
        full_prompt = command.get("full_prompt") or ""
        cmd_text = " ".join(_truncate(part, 40) for part in cmd[:8])
        return Panel(
            Text(cmd_text + "\n\n" + _truncate(full_prompt, 400)),
            title="Command", border_style="cyan",
        )

    @staticmethod
    def _render_metrics(session: Dict[str, Any]) -> RenderableType:
        table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
        table.add_column()
        table.add_column(justify="right")
        cost = _fmt_cost(session.get("last_total_cost_usd"))
        duration = _fmt_duration(session.get("last_duration_seconds"))
        started = _fmt_ts(session.get("started_at"))
        completed = _fmt_ts(session.get("last_completed_at"))
        table.add_row(f"started {started}", f"elapsed {duration}")
        table.add_row(f"completed {completed}", f"cost {cost}")
        table.add_row(f"attempt {session.get('attempt') or 1}",
                      f"workdir_exists={session.get('workdir_exists')}")
        return table

    @staticmethod
    def _render_chat(chat: Optional[Dict[str, Any]]) -> RenderableType:
        turns = (chat or {}).get("turns") or []
        if not turns:
            return Panel(
                Text("(no conversation yet — chat JSONL not present)", style="dim"),
                title="Conversation", border_style="dim",
            )
        table = Table(expand=True, padding=(0, 1), show_header=False)
        table.add_column("when", style="dim")
        table.add_column("role")
        table.add_column("preview")
        for turn in turns[-8:]:
            role = turn.get("role") or turn.get("type")
            marker = "▶" if turn.get("in_progress") else " "
            if role == "tool":
                preview = f"{turn.get('tool_name')}({_truncate(turn.get('tool_input'), 40)})"
            else:
                preview = _truncate(turn.get("content") or turn.get("summary") or "", 70)
            table.add_row(
                _fmt_ts(turn.get("ts") if isinstance(turn.get("ts"), (int, float)) else None),
                f"{marker} {role or '?'}",
                preview,
            )
        return Panel(table, title="Conversation", border_style="cyan")


# --------------------------------------------------------------------------- #
# History (screen 3)
# --------------------------------------------------------------------------- #


@dataclass
class HistoryData:
    sessions: List[Dict[str, Any]]
    api_errors: List[str]


class HistoryScreen:
    """Renderer for *Task History* — chronological list of dispatches."""

    def render(self, data: HistoryData) -> RenderableType:
        if not data.sessions:
            return Panel(
                Text("(no historical sessions)", style="dim"),
                title="TASK HISTORY", border_style="dim",
            )
        table = Table(expand=True, padding=(0, 1))
        table.add_column("Task", style="cyan")
        table.add_column("Stage")
        table.add_column("Branch")
        table.add_column("Done", justify="right")
        table.add_column("Duration", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("✓", justify="center")
        for s in data.sessions[:20]:
            ok = not (s.get("last_is_error") or False)
            mark = "✓" if ok else ("↻" if s.get("attempt", 1) > 1 else "✗")
            table.add_row(
                _truncate(s.get("task_id"), 12),
                _truncate(s.get("stage"), 12),
                _truncate(s.get("branch"), 22),
                _fmt_ts(s.get("last_completed_at")),
                _fmt_duration(s.get("last_duration_seconds")),
                _fmt_cost(s.get("last_total_cost_usd")),
                mark,
            )
        return Panel(table, title="TASK HISTORY", border_style="cyan")


# --------------------------------------------------------------------------- #
# Convenience renderer for tests
# --------------------------------------------------------------------------- #


def render_to_string(screen: Any, data: Any, *, width: int = 120) -> str:
    """Render a screen into a string for tests / debugging.

    The fixed-width ``Console.capture()`` is the test-only escape hatch
    permitted by pillar 03 §15 — production callers always render against
    the live console (auto-width).
    """
    console = Console(width=width, record=True)
    with console.capture() as cap:
        console.print(screen.render(data))
    return cap.get()
