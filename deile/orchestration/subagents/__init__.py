"""Sub-DEILEs paralelos em sessão CLI (issue #257).

Decomposição autônoma: o DEILE principal identifica sub-tarefas independentes
durante a conversa interativa e dispara N sub-DEILEs em paralelo (cada um com
contexto/sessão limpa), com painel multiplexado ao vivo, foco básico por tecla
numérica, e consolidação final.

Camadas:
    * :mod:`.events`        — ``SubAgentEvent`` + ``SubAgentState`` (dataclasses).
    * :mod:`.runner`        — :class:`SubAgentRunner` protocol + ``Local``/``Worker``
                              implementations.
    * :mod:`.orchestrator`  — :class:`SubAgentOrchestrator` — ``asyncio.gather``
                              com ``return_exceptions=True``, callback de progresso.

A UI (Rich Live multiplexada) vive em :mod:`deile.ui.subagent_panel`; a tool
LLM-facing em :mod:`deile.tools.dispatch_parallel_subagents`.
"""

from .constants import HISTORY_MARKER_KEY, is_display_only_entry
from .events import (SubAgentEvent, SubAgentEventKind, SubAgentState,
                     SubAgentStatus, SubAgentTask)
from .orchestrator import (MAX_SUBAGENT_BUDGET_S, SubAgentOrchestrator,
                           SubAgentResult)
from .runner import (LocalSubAgentRunner, SubAgentRunner, WorkerSubAgentRunner,
                     resolve_runner)

__all__ = [
    "HISTORY_MARKER_KEY",
    "LocalSubAgentRunner",
    "MAX_SUBAGENT_BUDGET_S",
    "SubAgentEvent",
    "SubAgentEventKind",
    "SubAgentOrchestrator",
    "SubAgentResult",
    "SubAgentRunner",
    "SubAgentState",
    "SubAgentStatus",
    "SubAgentTask",
    "WorkerSubAgentRunner",
    "is_display_only_entry",
    "resolve_runner",
]
