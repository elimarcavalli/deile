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

Round 2 (post-feedback):
    * Captura ``sys.stdout`` real ANTES do orquestrador redirecionar — passa
      ao renderer factory para que o painel apareça no terminal mesmo com
      ``print()`` dos sub-DEILEs suprimido.
    * Após o painel fechar, grava entrada ``role=assistant`` com markdown do
      resumo na ``conversation_history`` da sessão — sobrevive ao ``/resume``.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections import defaultdict
from typing import Dict, List

from deile.orchestration.subagents import (HISTORY_MARKER_KEY,
                                           MAX_SUBAGENT_BUDGET_S,
                                           SubAgentOrchestrator, SubAgentTask,
                                           resolve_runner)
from deile.orchestration.subagents.events import SubAgentState

from .base import (SecurityLevel, Tool, ToolCategory, ToolContext, ToolResult,
                   ToolSchema)

# Recursion guard (issue #257 "decomposição recursiva — fora de escopo").
# ContextVar herda no ``asyncio.create_task``, então sub-DEILEs spawnados via
# LocalSubAgentRunner enxergam ``_NESTING_DEPTH > 0`` e refusam nova chamada
# à tool. Reset acontece automaticamente quando o context manager sai.
_NESTING_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "dispatch_parallel_subagents.nesting", default=0
)

logger = logging.getLogger(__name__)


# Personas que o runner aceita (espelha WorkerPersona em deile_worker_client).
_ALLOWED_PERSONAS = frozenset({"developer", "architect", "debugger", "reviewer", "analyst"})

# Limites defensivos do schema.
_MIN_SUBTASKS = 2
_MAX_SUBTASKS = 5
_DESCRIPTION_MAX_LEN = 80
_PROMPT_MIN_LEN = 30
_PROMPT_MAX_LEN = 8000

# HISTORY_MARKER_KEY mora em deile/orchestration/subagents/constants.py
# (re-exportado acima) — único ponto de verdade, lido por replay_history,
# build_context (filtragem) e esta tool (escrita).


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

            # Recursion guard: a issue #257 explicitamente exclui decomposição
            # recursiva. Se ESTE tool já está sendo executado no contexto
            # atual (ou seja, um sub-DEILE local tentando se decompor de novo),
            # rejeita antes de qualquer outro trabalho.
            if _NESTING_DEPTH.get() > 0:
                return ToolResult.error_result(
                    "dispatch_parallel_subagents cannot be nested — você já está "
                    "rodando dentro de um sub-DEILE. Faça o trabalho diretamente "
                    "com as outras ferramentas; decomposição recursiva está fora "
                    "de escopo (issue #257).",
                    error_code="RECURSION_DENIED",
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

            # Renderer factory — assina (states, broadcast, real_stdout). O
            # orquestrador captura sys.stdout REAL antes do redirect e passa
            # aqui, para que o painel apareça no terminal mesmo enquanto os
            # ``print()`` dos sub-DEILEs estão sendo desviados pro buffer.
            host_console = context.session_data.get("_console")
            renderer_factory = None
            if host_console is not None:
                def _make_renderer(states, broadcast, real_stdout=None):
                    from deile.ui.subagent_panel import SubAgentPanelRenderer
                    return SubAgentPanelRenderer(
                        host_console,
                        states,
                        broadcast,
                        real_stdout=real_stdout,
                    )
                renderer_factory = _make_renderer

            orchestrator = SubAgentOrchestrator(
                runner,
                max_parallel=max_parallel,
                renderer_factory=renderer_factory,
                capture_output=True,
            )

            logger.info(
                "dispatch_parallel_subagents: spawning %d sub-DEILEs (runner=%s, parallel=%d)",
                len(tasks),
                runner.__class__.__name__,
                max_parallel,
            )

            # Eleva profundidade de nesting — sub-DEILEs spawnados via
            # asyncio.create_task herdam este ContextVar e bloqueiam recursão.
            token = _NESTING_DEPTH.set(_NESTING_DEPTH.get() + 1)
            try:
                result = await orchestrator.run(tasks)
            finally:
                _NESTING_DEPTH.reset(token)

            # ── Persistência no histórico (issue #257 round 2, fix #2) ──────
            # Grava uma entrada role=assistant com o markdown do resumo do
            # painel. ``replay_history`` (cli_session_helpers.py) detecta o
            # marcador HISTORY_MARKER_KEY e renderiza no /resume, mesmo
            # quando o LLM não escreveu consolidação textual.
            try:
                self._persist_to_history(agent, session_id, result, len(tasks))
            except Exception:
                # Persistência é best-effort — não vale a pena falhar a tool
                # por causa de um histórico ausente (sessão recém-criada,
                # agente sem sessão registrada, etc.).
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

    @classmethod
    def _prune_expired(cls, now: float) -> None:
        cutoff = cls._DISPATCH_COOLDOWN_S * 10
        stale = [sid for sid, ts in cls._LAST_DISPATCH.items() if (now - ts) > cutoff]
        for sid in stale:
            cls._LAST_DISPATCH.pop(sid, None)

    @staticmethod
    def _persist_to_history(agent, session_id: str, result, n_tasks: int) -> None:
        """Escreve o resumo do painel na conversation_history da sessão.

        Adiciona uma entrada ``role=assistant`` com o markdown_summary do
        resultado e metadata flag ``HISTORY_MARKER_KEY=True``. Ao ``/resume``,
        :func:`replay_history` reconhece a flag e renderiza, mesmo quando o
        LLM principal não escreveu nenhuma consolidação por conta própria.
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
        markdown = result.markdown_summary()
        add_to_history(
            "assistant",
            markdown,
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


def _safe_truncate_markdown(text: str, max_chars: int = 400) -> str:
    """Trunca preservando integridade básica de markdown.

    O ``result_text`` do LocalRunner é a concatenação de TEXT_DELTAs do
    sub-DEILE — pode conter code-fences abertas, links, listas. Cortar em
    400 chars no meio de um ``[abc](http://...)`` ou de um ` ``` ` solto
    polui o contexto do LLM principal (que pode tentar consolidar e usar
    a sintaxe quebrada).

    Estratégia: corta no último ``\\n\\n`` ou ``.`` antes do limite. Se não
    achar, corta no limite cru com ``…``. Code-fences abertas são fechadas
    com um ``\\n```\\n`` defensivo.
    """
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    # Procura a última quebra "boa" dentro de [0..max_chars]:
    # \n\n (parágrafo) > ". " (sentença). Sem floor — se a única quebra estiver
    # no início, mesmo assim entregamos um corte clean (melhor que cortar no
    # meio de markdown).
    cut = text.rfind("\n\n", 0, max_chars)
    if cut < 0:
        cut = text.rfind(". ", 0, max_chars)
        if cut > 0:
            cut += 1  # inclui o ponto
    if cut < 0:
        cut = max_chars - 1
        truncated = text[:cut].rstrip() + "…"
    else:
        truncated = text[:cut].rstrip()
    # Se ficou com code-fence aberta (número ímpar de ```), fecha.
    if truncated.count("```") % 2 == 1:
        truncated += "\n```"
    return truncated


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
        "summary": _safe_truncate_markdown(st.result_text, 400),
    }


__all__ = [
    "DispatchParallelSubagentsTool",
    "HISTORY_MARKER_KEY",
    "_build_tasks_from_payload",
]
