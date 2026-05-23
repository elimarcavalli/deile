"""Eventos e estado dos sub-DEILEs paralelos (issue #257).

Tipos *locais ao escopo* da invocação do tool ``dispatch_parallel_subagents``.
Não usamos o ``EventBus`` global porque a vida útil destes eventos é uma única
execução da tool (não há outros assinantes interessados além do renderer
multipanel) e o ``EventBus`` adiciona overhead de fila/dispatch desnecessário
para um broadcast 1→1.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, List, Literal, Optional


SubAgentStatus = Literal["pending", "running", "ok", "error", "cancelled"]


class SubAgentEventKind(Enum):
    """Tipo de evento emitido por um runner para o orquestrador/renderer."""

    STARTED = "started"
    TOOL = "tool"                    # tool call em curso (TOOL_USE_END no stream)
    TOOL_RESULT = "tool_result"      # tool concluiu (ok/erro)
    TEXT = "text"                    # primeira linha não-vazia do TEXT_DELTA
    PROGRESS = "progress"            # phase update (vinda do worker, p.ex.)
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SubAgentEvent:
    """Mensagem emitida pelo runner para o orquestrador (e renderer)."""

    kind: SubAgentEventKind
    index: int                        # 1-based, casa com SubAgentTask.index
    label: str = ""                   # texto curto, ≤120 chars
    tool_name: Optional[str] = None
    tool_status: Optional[str] = None  # "success" | "error"
    file_path: Optional[str] = None   # quando a tool tocou um arquivo
    error: Optional[str] = None
    extra: Optional[dict] = None


@dataclass
class SubAgentTask:
    """Definição imutável de uma sub-tarefa a ser disparada.

    A ``index`` é 1-based porque vira o atalho de teclado para foco
    (``1`` foca a frente #1, etc.) — assim como Claude Code numera tool calls.
    """

    index: int
    description: str
    prompt: str
    persona: Optional[str] = None
    model: Optional[str] = None


@dataclass
class SubAgentState:
    """Estado mutável de uma sub-tarefa, observado pelo renderer.

    Os campos são todos atualizados de UM SÓ produtor (o runner que possui
    esta ``state``) — sem locks. O renderer só *lê* e renderiza snapshots.
    """

    task: SubAgentTask
    status: SubAgentStatus = "pending"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # Bounded deque para não crescer indefinidamente em tarefas longas.
    progress_lines: Deque[str] = field(default_factory=lambda: deque(maxlen=30))
    current_activity: Optional[str] = None
    files_touched: List[str] = field(default_factory=list)
    result_text: str = ""
    error: Optional[str] = None
    task_id: Optional[str] = None     # populado só pelo WorkerSubAgentRunner

    @property
    def elapsed_s(self) -> float:
        """Segundos desde ``started_at`` (ou 0 se ainda não iniciou).

        Quando a task já terminou usa ``finished_at`` como teto — evita o
        contador continuar correndo no painel após o término.
        """
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("ok", "error", "cancelled")

    def push_progress(self, line: str) -> None:
        """Acrescenta linha ao histórico (bounded) e atualiza activity."""
        if not line:
            return
        line = line[:120]
        self.progress_lines.append(line)
        self.current_activity = line

    def add_file(self, path: Any) -> None:
        """Registra arquivo tocado, dedup-aware."""
        if not path:
            return
        p = str(path)
        if p not in self.files_touched:
            self.files_touched.append(p)


__all__ = [
    "SubAgentEvent",
    "SubAgentEventKind",
    "SubAgentStatus",
    "SubAgentState",
    "SubAgentTask",
]
