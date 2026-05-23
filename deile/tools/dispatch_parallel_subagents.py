"""dispatch_parallel_subagents — decomposição autônoma em sub-DEILEs paralelos.

Issue #257. Durante uma sessão interativa CLI, o LLM identifica sub-tarefas
**substanciais e independentes** dentro do pedido do usuário e chama esta tool
para disparar N sub-DEILEs em paralelo. Cada sub-DEILE recebe uma sessão limpa
(contexto/histórico próprios), executa autonomamente e devolve seu resultado;
a tool agrega tudo e devolve um resumo consolidado ao LLM principal — que
escreve a consolidação final ao usuário.

A UX ao vivo (painel multipanel ~5 linhas/frente, foco por tecla numérica) é
renderizada por :class:`deile.ui.subagent_panel.SubAgentPanelRenderer`. A
execução real fica a cargo de :class:`deile.orchestration.subagents.\
SubAgentOrchestrator`, que escolhe entre :class:`LocalSubAgentRunner` (default,
in-process) ou :class:`WorkerSubAgentRunner` (delega ao ``deile-worker``).

Heurística da persona, NÃO desta tool (mantemos a tool burra):
    * usar quando ≥2 frentes independentes e substanciais;
    * NÃO usar quando há dependência sequencial / micro-tarefa / pedido
      explícito de passo-a-passo.

Esta tool valida apenas o shape do payload (2-5 subtasks, prompts não-vazios,
descriptions únicas) e roda um anti-loop curto (5s) por ``session_id`` para
o LLM não chamar duas vezes seguidas.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional

from deile.orchestration.subagents import (MAX_SUBAGENT_BUDGET_S,
                                           SubAgentOrchestrator,
                                           SubAgentTask, resolve_runner)
from deile.orchestration.subagents.events import SubAgentState

from .base import (SecurityLevel, Tool, ToolCategory, ToolContext, ToolResult,
                   ToolSchema)

logger = logging.getLogger(__name__)


# Personas que o runner aceita (espelha WorkerPersona em deile_worker_client).
_ALLOWED_PERSONAS = frozenset({"developer", "architect", "debugger", "reviewer", "analyst"})

# Limites defensivos do schema.
_MIN_SUBTASKS = 2
_MAX_SUBTASKS = 5
_DESCRIPTION_MAX_LEN = 80
_PROMPT_MIN_LEN = 30
_PROMPT_MAX_LEN = 8000


class DispatchParallelSubagentsTool(Tool):
    """Dispara N sub-DEILEs paralelos com painel ao vivo + consolidação."""

    # Anti-loop por sessão (5s). Cooldown curto porque o caso legítimo de
    # disparar 2x seguidas é raro; o objetivo é só evitar que o LLM, ao ver
    # o resumo, "ache" que precisa retentar.
    _LAST_DISPATCH: Dict[str, float] = {}
    _DISPATCH_COOLDOWN_S: float = 5.0
    _SESSION_LOCKS: "Dict[str, asyncio.Lock]" = defaultdict(asyncio.Lock)

    @property
    def name(self) -> str:
        return "dispatch_parallel_subagents"

    @property
    def description(self) -> str:
        return (
            "Decompose o pedido em N sub-tarefas SUBSTANCIAIS e INDEPENDENTES "
            "executadas em paralelo, cada uma num sub-DEILE com contexto limpo. "
            "Use SOMENTE quando: (a) há ≥2 frentes verdadeiramente independentes; "
            "(b) cada frente é substancial (refator multi-arquivo, geração de "
            "testes, doc longa); (c) o usuário não pediu passo-a-passo. NÃO use "
            "para micro-tarefas nem para passos sequenciais. Retorna um resumo "
            "consolidado — o usuário já viu o progresso ao vivo no painel."
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
                                        "description": (
                                            "Persona do sub-DEILE (default: developer)."
                                        ),
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
                            },
                        },
                    },
                },
                required=["subtasks"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
                max_execution_time=int(MAX_SUBAGENT_BUDGET_S),
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

            # Validação defensiva (defense-in-depth: o schema do JSON-Schema já
            # checa minItems/maxItems, mas alguns providers são mais frouxos).
            tasks, validation_error = _build_tasks_from_payload(raw_subtasks)
            if validation_error:
                return ToolResult.error_result(
                    validation_error, error_code="BAD_REQUEST"
                )

            # Anti-loop por sessão (atômico via lock).
            async with self._SESSION_LOCKS[session_id]:
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

            # Resolve agent + runner + orquestrador.
            agent = context.session_data.get("_agent")
            if agent is None:
                # Fallback: tenta resolver via singleton — não há registry global
                # do agente, então deixamos o erro claro pro caller.
                return ToolResult.error_result(
                    "agent reference not in context.session_data['_agent']; "
                    "cannot spawn sub-agents",
                    error_code="AGENT_NOT_AVAILABLE",
                )

            from deile.config.settings import get_settings
            settings = get_settings()
            max_parallel = min(
                int(getattr(settings, "subagent_max_parallel", 3)),
                len(tasks),
            )

            runner = resolve_runner(agent, session_id=session_id)

            # Renderer factory — opcional, evita importar UI nos testes
            # headless (passamos console=None para desabilitar).
            console = context.session_data.get("_console")
            renderer_factory = None
            if console is not None:
                def _make_renderer(states: List[SubAgentState], broadcast):
                    from deile.ui.subagent_panel import SubAgentPanelRenderer
                    return SubAgentPanelRenderer(console, states, broadcast)
                renderer_factory = _make_renderer

            orchestrator = SubAgentOrchestrator(
                runner,
                max_parallel=max_parallel,
                renderer_factory=renderer_factory,
            )

            logger.info(
                "dispatch_parallel_subagents: spawning %d sub-DEILEs (runner=%s, parallel=%d)",
                len(tasks),
                runner.__class__.__name__,
                max_parallel,
            )
            result = await orchestrator.run(tasks)

            return ToolResult.success_result(
                data={
                    "ok_global": result.ok_global,
                    "ok_count": result.ok_count,
                    "error_count": result.error_count,
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

    @classmethod
    def _prune_expired(cls, now: float) -> None:
        cutoff = cls._DISPATCH_COOLDOWN_S * 10
        stale = [sid for sid, ts in cls._LAST_DISPATCH.items() if (now - ts) > cutoff]
        for sid in stale:
            cls._LAST_DISPATCH.pop(sid, None)


def _build_tasks_from_payload(raw: object):
    """Valida payload e devolve ``(tasks, error_msg)``.

    Reusable for unit-tests directly without instantiating the tool.
    """
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
        if not isinstance(item, dict):
            return [], f"subtasks[{i-1}] must be an object"
        description = str(item.get("description") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        persona = item.get("persona")
        model = item.get("model")

        if not description:
            return [], f"subtasks[{i-1}].description is required"
        if len(description) > _DESCRIPTION_MAX_LEN:
            return [], (
                f"subtasks[{i-1}].description exceeds {_DESCRIPTION_MAX_LEN} chars"
            )
        if not prompt:
            return [], f"subtasks[{i-1}].prompt is required"
        if len(prompt) < _PROMPT_MIN_LEN:
            return [], (
                f"subtasks[{i-1}].prompt is too short (<{_PROMPT_MIN_LEN} chars) — "
                "use a substantial sub-task or run sequentially"
            )
        if len(prompt) > _PROMPT_MAX_LEN:
            return [], (
                f"subtasks[{i-1}].prompt exceeds {_PROMPT_MAX_LEN} chars"
            )
        # Personas/models opcionais — quando presentes, validar.
        if persona is not None:
            persona = str(persona).strip().lower()
            if persona not in _ALLOWED_PERSONAS:
                return [], (
                    f"subtasks[{i-1}].persona '{persona}' invalid; "
                    f"allowed: {sorted(_ALLOWED_PERSONAS)}"
                )
        else:
            persona = None
        if model is not None:
            model = str(model).strip() or None

        # Description única por chamada — evita confusão visual no painel.
        desc_key = description.lower()
        if desc_key in seen_desc:
            return [], (
                f"subtasks[{i-1}].description duplicates an earlier subtask — "
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


def _state_to_dict(st: SubAgentState) -> dict:
    """Serializa um :class:`SubAgentState` para o payload de retorno do tool.

    Mantemos curto: o LLM principal não precisa ver result_text completo (já
    viu o painel). Para tasks que falharam, incluímos ``error`` integral.
    """
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
        "summary": st.result_text[:400] if st.result_text else "",
    }


__all__ = [
    "DispatchParallelSubagentsTool",
    "_build_tasks_from_payload",
]
