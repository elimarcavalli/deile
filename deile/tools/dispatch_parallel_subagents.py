"""dispatch_parallel_subagents — decomposição autônoma em sub-DEILEs paralelos.

Issue #257. Durante uma sessão CLI, o LLM identifica sub-tarefas **substanciais
e independentes** dentro do pedido e chama esta tool para disparar N sub-DEILEs
em paralelo. Cada sub-DEILE recebe sessão limpa, executa autonomamente, e
devolve seu resultado; a tool agrega tudo e devolve um resumo consolidado ao
LLM principal.

Heurística da persona (NÃO desta tool — mantemos a tool burra):
    * usar quando ≥2 frentes independentes e substanciais;
    * NÃO usar quando há dependência sequencial / micro-tarefa / passo-a-passo.

Esta tool valida apenas o shape do payload (2-5 subtasks, prompts não-vazios,
descriptions únicas) e roda um anti-loop curto (5s) por ``session_id``.

Persistência: após o painel fechar, grava entrada ``role=assistant`` com
markdown do resumo na ``conversation_history`` da sessão — sobrevive ao
``/resume`` (renderizado por :func:`replay_history` via ``HISTORY_MARKER_KEY``).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from collections import OrderedDict
from typing import Dict, List, Optional

from deile.config.settings import get_settings
from deile.orchestration.subagents import (HISTORY_MARKER_KEY,
                                           SubAgentOrchestrator, SubAgentTask,
                                           resolve_runner)
from deile.orchestration.subagents.events import SubAgentState
from deile.orchestration.subagents.orchestrator import (_get_budget_s,
                                                        _lazy_lock_for_loop)

from .base import (SecurityLevel, Tool, ToolCategory, ToolContext, ToolResult,
                   ToolSchema)

# Recursion guard — ContextVar herda no ``asyncio.create_task``, então sub-DEILEs
# locais herdam ``_NESTING_DEPTH>0`` e refusam nova chamada à tool.
_NESTING_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "dispatch_parallel_subagents.nesting", default=0
)

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _nesting_depth_inc():
    """Incrementa ``_NESTING_DEPTH`` com reset garantido (Pilar 03 §1)."""
    token = _NESTING_DEPTH.set(_NESTING_DEPTH.get() + 1)
    try:
        yield
    finally:
        _NESTING_DEPTH.reset(token)


# Personas aceitas pelo runner (espelha WorkerPersona).
_ALLOWED_PERSONAS = frozenset({"developer", "architect", "debugger", "reviewer", "analyst"})

# Limites defensivos do schema.
_MIN_SUBTASKS = 2
_MAX_SUBTASKS = 5
_DESCRIPTION_MAX_LEN = 80
_PROMPT_MIN_LEN = 30
_PROMPT_MAX_LEN = 8000


class DispatchParallelSubagentsTool(Tool):
    """Dispara N sub-DEILEs paralelos com painel ao vivo + consolidação."""

    # Anti-loop por sessão (5s) — evita LLM retentando após ver o resumo.
    _LAST_DISPATCH: Dict[str, float] = {}
    _DISPATCH_COOLDOWN_S: float = 5.0
    # LRU cap evita vazamento sob carga prolongada (sessões fechadas deixariam
    # locks órfãos).
    _SESSION_LOCKS: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
    _SESSION_LOCKS_MAX: int = 256
    # Lock-guarda lazy-bound ao event loop corrente (testes multi-loop).
    _SESSION_LOCKS_GUARD: Optional[asyncio.Lock] = None
    _SESSION_LOCKS_GUARD_LOOP_ID: Optional[int] = None

    @property
    def name(self) -> str:
        return "dispatch_parallel_subagents"

    @property
    def description(self) -> str:
        return (
            "Decompose o pedido em N sub-tarefas SUBSTANCIAIS e INDEPENDENTES "
            "executadas em paralelo, cada uma num sub-DEILE com contexto limpo. "
            "FUNCIONA SEMPRE — runner local (in-process, asyncio) é o default; "
            "NÃO requer worker pod, K8s ou qualquer infra externa. Não confunda "
            "com dispatch_deile_task (ferramenta do bot que SIM precisa do "
            "deile-worker). Use SOMENTE quando: (a) há ≥2 frentes verdadeiramente "
            "independentes; (b) cada frente é substancial (refator multi-arquivo, "
            "geração de testes, doc longa); (c) o usuário não pediu passo-a-passo. "
            "NÃO use para micro-tarefas nem para passos sequenciais. Retorna um "
            "resumo consolidado — o usuário já viu o progresso ao vivo no painel."
        )

    @property
    def category(self) -> str:
        return ToolCategory.SYSTEM.value

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name=self.name,
                description=self.description,
                parameters={
                    "type": "object",
                    "properties": {
                        "subtasks": {
                            "type": "array",
                            "minItems": _MIN_SUBTASKS,
                            "maxItems": _MAX_SUBTASKS,
                            "description": (
                                f"Lista de {_MIN_SUBTASKS}-{_MAX_SUBTASKS} sub-tarefas "
                                "independentes. Cada item tem description+prompt; "
                                "persona/model são opcionais."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "description": {
                                        "type": "string",
                                        "description": (
                                            f"Resumo curto (≤{_DESCRIPTION_MAX_LEN} chars) — "
                                            "vira o título do painel da frente."
                                        ),
                                    },
                                    "prompt": {
                                        "type": "string",
                                        "description": (
                                            "Prompt completo e AUTO-CONTIDO injetado no "
                                            "sub-DEILE (ele não vê a conversa principal). "
                                            f"Min {_PROMPT_MIN_LEN}, max {_PROMPT_MAX_LEN} chars."
                                        ),
                                    },
                                    "persona": {
                                        "type": "string",
                                        "enum": sorted(_ALLOWED_PERSONAS),
                                        "description": "Persona do sub-DEILE (default: developer).",
                                    },
                                    "model": {
                                        "type": "string",
                                        "description": (
                                            "Override opcional do modelo (e.g. "
                                            "'deepseek:deepseek-v4-pro')."
                                        ),
                                    },
                                },
                                "required": ["description", "prompt"],
                                "additionalProperties": False,
                            },
                        },
                    },
                },
                required=["subtasks"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
                # Budget lido em runtime (respeita override por env/settings).
                max_execution_time=int(_get_budget_s()),
            )
        )

    async def execute(self, context: ToolContext) -> ToolResult:
        try:
            args = dict(context.parsed_args or {})
            raw_subtasks = args.get("subtasks") or []
            session_id = (
                context.session_data.get("session_id")
                or context.session_data.get("_session_id")
                or "default"
            )

            # Recursion guard antes de qualquer outro trabalho.
            if _NESTING_DEPTH.get() > 0:
                return ToolResult.error_result(
                    "dispatch_parallel_subagents cannot be nested — você já está "
                    "rodando dentro de um sub-DEILE. Faça o trabalho diretamente "
                    "com as outras ferramentas; decomposição recursiva está fora "
                    "de escopo (issue #257).",
                    error_code="RECURSION_DENIED",
                )

            tasks, validation_error = _build_tasks_from_payload(raw_subtasks)
            if validation_error:
                return ToolResult.error_result(validation_error, error_code="BAD_REQUEST")

            # Anti-loop atômico por sessão.
            session_lock = await self._get_session_lock(session_id)
            async with session_lock:
                now = time.monotonic()
                self._prune_expired(now)
                last = self._LAST_DISPATCH.get(session_id)
                if last is not None and (now - last) < self._DISPATCH_COOLDOWN_S:
                    remaining = self._DISPATCH_COOLDOWN_S - (now - last)
                    return ToolResult.error_result(
                        f"dispatch_parallel_subagents já foi chamado há {now-last:.0f}s; "
                        f"aguarde {remaining:.1f}s ou explique ao usuário o resultado "
                        f"da chamada anterior. NÃO chame de novo esperando resultado "
                        f"diferente.",
                        error_code="DISPATCH_COOLDOWN",
                    )
                self._LAST_DISPATCH[session_id] = now

            agent = context.session_data.get("_agent")
            if agent is None:
                return ToolResult.error_result(
                    "agent reference not in context.session_data['_agent']; "
                    "cannot spawn sub-agents",
                    error_code="AGENT_NOT_AVAILABLE",
                )

            settings = get_settings()
            max_parallel = min(
                int(getattr(settings, "subagent_max_parallel", 3)),
                len(tasks),
            )

            runner = resolve_runner(agent, session_id=session_id)
            renderer_factory = self._build_renderer_factory(
                context.session_data.get("_console")
            )

            orchestrator = SubAgentOrchestrator(
                runner,
                max_parallel=max_parallel,
                renderer_factory=renderer_factory,
                capture_output=True,
            )

            logger.info(
                "dispatch_parallel_subagents: spawning %d sub-DEILEs (runner=%s, parallel=%d)",
                len(tasks), runner.__class__.__name__, max_parallel,
            )

            audit_details = {
                "n_subtasks": len(tasks),
                "runner": runner.__class__.__name__,
                "max_parallel": max_parallel,
            }
            # Admission audit. Terminal evento sai no ``finally`` com desfecho real.
            self._audit_emit(
                action="dispatch", result="accepted",
                session_id=session_id, details=audit_details,
            )

            result = None
            audit_result = "failure"
            try:
                with _nesting_depth_inc():
                    result = await orchestrator.run(tasks)
                if result.cancelled:
                    audit_result = "cancelled"
                elif result.ok_global:
                    audit_result = "success"
                else:
                    audit_result = "failure"
            except asyncio.CancelledError:
                # Parent cancel: refletir desfecho real antes do re-raise (Pilar 03 §6).
                audit_result = "cancelled"
                raise
            finally:
                terminal_details = dict(audit_details)
                if result is not None:
                    terminal_details.update({
                        "ok_count": result.ok_count,
                        "error_count": result.error_count,
                        "elapsed_s": round(result.elapsed_s, 3),
                        "cancelled": result.cancelled,
                    })
                    if any(
                        (st.error or "").strip() == "subagent_budget_exceeded"
                        for st in result.states
                    ):
                        audit_result = "budget_exceeded"
                self._audit_emit(
                    action="dispatch", result=audit_result,
                    session_id=session_id, details=terminal_details,
                )

            # Persistência best-effort para /resume.
            try:
                self._persist_to_history(agent, session_id, result, len(tasks))
            except Exception:
                logger.debug("persist_to_history failed", exc_info=True)

            return ToolResult.success_result(
                data={
                    "ok_global": result.ok_global,
                    "ok_count": result.ok_count,
                    "error_count": result.error_count,
                    "cancelled": result.cancelled,
                    "elapsed_s": result.elapsed_s,
                    "subtasks": [_state_to_dict(s) for s in result.states],
                },
                message=result.consolidated_summary(),
            )

        except Exception as exc:  # noqa: BLE001 — Tool contract top-level guard
            logger.exception("dispatch_parallel_subagents failed unexpectedly")
            return ToolResult.error_result(
                f"unexpected error: {exc}", error=exc, error_code="INTERNAL_ERROR"
            )

    @staticmethod
    def _build_renderer_factory(host_console):
        """Constrói factory ``(states, broadcast, real_stdout) -> renderer``.

        ``None`` quando não há console (headless / fixture). Import tardio
        de :mod:`deile.ui.subagent_panel` evita acoplar a tool à UI no import.
        """
        if host_console is None:
            return None

        def _make_renderer(states, broadcast, real_stdout=None):
            from deile.ui.subagent_panel import SubAgentPanelRenderer
            return SubAgentPanelRenderer(
                host_console, states, broadcast, real_stdout=real_stdout,
            )
        return _make_renderer

    def _audit_emit(
        self, *, action: str, result: str, session_id: str, details: dict,
    ) -> None:
        """Emit a typed audit event (best-effort, never raises). Pilar 8."""
        try:
            from deile.security.audit_logger import (AuditEventType,
                                                     SeverityLevel,
                                                     get_audit_logger)
            severity = (
                SeverityLevel.WARNING
                if result in ("failure", "budget_exceeded")
                else SeverityLevel.INFO
            )
            get_audit_logger().log_event(
                event_type=AuditEventType.TOOL_EXECUTION,
                severity=severity,
                actor="tool",  # papel — identidade específica em tool_name
                resource=f"session:{session_id}",
                action=action,
                result=result,
                details=details,
                tool_name=self.name,
            )
        except Exception:
            logger.debug("audit emission failed", exc_info=True)

    @classmethod
    def _prune_expired(cls, now: float) -> None:
        cutoff = cls._DISPATCH_COOLDOWN_S * 10
        stale = [sid for sid, ts in cls._LAST_DISPATCH.items() if (now - ts) > cutoff]
        for sid in stale:
            cls._LAST_DISPATCH.pop(sid, None)

    @classmethod
    def _get_locks_guard(cls) -> asyncio.Lock:
        """Lazy-init do lock-guarda por event loop. Compartilha helper com orchestrator."""
        cls._SESSION_LOCKS_GUARD, cls._SESSION_LOCKS_GUARD_LOOP_ID = _lazy_lock_for_loop(
            cls._SESSION_LOCKS_GUARD, cls._SESSION_LOCKS_GUARD_LOOP_ID
        )
        return cls._SESSION_LOCKS_GUARD

    @classmethod
    async def _get_session_lock(cls, session_id: str) -> asyncio.Lock:
        """Per-session lock; LRU-bounded em ``_SESSION_LOCKS_MAX``.

        Eviction: itera oldest → newest, pula travados, NUNCA evict o sid recém-tocado
        (concurrent caller para o mesmo sid criaria lock novo, quebrando mutex).
        Se TODOS estiverem travados, aceita overshoot transitório.
        """
        async with cls._get_locks_guard():
            lock = cls._SESSION_LOCKS.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                cls._SESSION_LOCKS[session_id] = lock
            else:
                cls._SESSION_LOCKS.move_to_end(session_id)
            if len(cls._SESSION_LOCKS) > cls._SESSION_LOCKS_MAX:
                excess = len(cls._SESSION_LOCKS) - cls._SESSION_LOCKS_MAX
                removed = 0
                for sid in list(cls._SESSION_LOCKS.keys()):
                    if removed >= excess:
                        break
                    if sid == session_id:
                        continue
                    candidate = cls._SESSION_LOCKS.get(sid)
                    if candidate is None or candidate.locked():
                        continue
                    cls._SESSION_LOCKS.pop(sid, None)
                    removed += 1
                if removed < excess:
                    logger.warning(
                        "_SESSION_LOCKS over cap (%d > %d); %d still locked, "
                        "accepting transitory overshoot",
                        len(cls._SESSION_LOCKS),
                        cls._SESSION_LOCKS_MAX,
                        excess - removed,
                    )
            return lock

    @staticmethod
    def _persist_to_history(agent, session_id: str, result, n_tasks: int) -> None:
        """Grava resumo no ``conversation_history`` com ``HISTORY_MARKER_KEY`` flag.

        ``replay_history`` (cli_session_helpers) detecta a flag e renderiza no
        ``/resume`` mesmo quando o LLM principal não escreveu consolidação textual.
        """
        sessions = getattr(agent, "_sessions", None)
        if not isinstance(sessions, dict):
            return
        session = sessions.get(session_id)
        if session is None:
            return
        add_to_history = getattr(session, "add_to_history", None)
        if not callable(add_to_history):
            return
        add_to_history(
            "assistant",
            result.markdown_summary(),
            {
                HISTORY_MARKER_KEY: True,
                "ok_count": result.ok_count,
                "error_count": result.error_count,
                "elapsed_s": result.elapsed_s,
                "n_subtasks": n_tasks,
                "cancelled": result.cancelled,
            },
        )


def _build_tasks_from_payload(raw: object):
    """Valida payload e devolve ``(tasks, error_msg)``. Reusable para testes diretos."""
    if not isinstance(raw, list):
        return [], "subtasks must be an array"
    if not (_MIN_SUBTASKS <= len(raw) <= _MAX_SUBTASKS):
        return [], (
            f"subtasks must contain between {_MIN_SUBTASKS} and {_MAX_SUBTASKS} items; "
            f"got {len(raw)}"
        )
    tasks: List[SubAgentTask] = []
    seen_desc: set = set()
    for i, item in enumerate(raw, start=1):
        idx = i - 1
        if not isinstance(item, dict):
            return [], f"subtasks[{idx}] must be an object"
        description = str(item.get("description") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        persona = item.get("persona")
        model = item.get("model")

        if not description:
            return [], f"subtasks[{idx}].description is required"
        if len(description) > _DESCRIPTION_MAX_LEN:
            return [], f"subtasks[{idx}].description exceeds {_DESCRIPTION_MAX_LEN} chars"
        if not prompt:
            return [], f"subtasks[{idx}].prompt is required"
        if len(prompt) < _PROMPT_MIN_LEN:
            return [], (
                f"subtasks[{idx}].prompt is too short (<{_PROMPT_MIN_LEN} chars) — "
                "use a substantial sub-task or run sequentially"
            )
        if len(prompt) > _PROMPT_MAX_LEN:
            return [], f"subtasks[{idx}].prompt exceeds {_PROMPT_MAX_LEN} chars"
        if persona is not None:
            persona = str(persona).strip().lower()
            if persona not in _ALLOWED_PERSONAS:
                return [], (
                    f"subtasks[{idx}].persona '{persona}' invalid; "
                    f"allowed: {sorted(_ALLOWED_PERSONAS)}"
                )
        else:
            persona = None
        if model is not None:
            model = str(model).strip() or None

        desc_key = description.lower()
        if desc_key in seen_desc:
            return [], (
                f"subtasks[{idx}].description duplicates an earlier subtask — "
                "each frente deve ser distinguível"
            )
        seen_desc.add(desc_key)

        tasks.append(SubAgentTask(
            index=i,
            description=description,
            prompt=prompt,
            persona=persona,
            model=model,
        ))
    return tasks, None


def _safe_truncate_markdown(text: str, max_chars: int = 400) -> str:
    """Trunca preservando integridade básica de markdown (parágrafo > sentença > corte cru).

    Code-fences abertas (`````) são fechadas com um defensivo ``\\n```\\n``.
    """
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n\n", 0, max_chars)
    if cut < 0:
        cut = text.rfind(". ", 0, max_chars)
        if cut > 0:
            cut += 1
    if cut < 0:
        cut = max_chars - 1
        truncated = text[:cut].rstrip() + "…"
    else:
        truncated = text[:cut].rstrip()
    if truncated.count("```") % 2 == 1:
        truncated += "\n```"
    return truncated


def _state_to_dict(st: SubAgentState) -> dict:
    """Serializa um :class:`SubAgentState` para o payload de retorno (LLM-facing)."""
    return {
        "index": st.task.index,
        "description": st.task.description,
        "persona": st.task.persona,
        "model": st.task.model,
        "status": st.status,
        "elapsed_s": st.elapsed_s,
        "files_touched": list(st.files_touched),
        "task_id": st.task_id,
        "error": st.error,
        "summary": _safe_truncate_markdown(st.result_text, 400),
    }


__all__ = [
    "DispatchParallelSubagentsTool",
    "HISTORY_MARKER_KEY",
    "_build_tasks_from_payload",
]
