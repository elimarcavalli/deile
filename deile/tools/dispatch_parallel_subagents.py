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
import contextlib
import contextvars
import logging
import time
from collections import OrderedDict
from typing import Dict, List

from deile.config.settings import get_settings
from deile.orchestration.subagents import (HISTORY_MARKER_KEY,
                                           SubAgentOrchestrator, SubAgentTask,
                                           resolve_runner)
from deile.orchestration.subagents._loop_lock import LoopBoundLock
from deile.orchestration.subagents.events import SubAgentState
from deile.orchestration.subagents.orchestrator import _get_budget_s

from ._dispatch_cooldown import is_in_cooldown, prune_expired, record_dispatch
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


@contextlib.contextmanager
def _nesting_depth_inc():
    """Context manager que incrementa ``_NESTING_DEPTH`` e garante reset.

    Wrapper sobre ``ContextVar.set/reset`` para que futuras edições não
    esqueçam o reset (Pilar 03 §1 — cleanup confiável).
    """
    token = _NESTING_DEPTH.set(_NESTING_DEPTH.get() + 1)
    try:
        yield
    finally:
        _NESTING_DEPTH.reset(token)


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
    # M5/M14 (PR #295 review): cap LRU para evitar crescimento sem limite.
    # Sessões fechadas deixavam locks órfãos no defaultdict; sob carga
    # prolongada (worker singleton com centenas de sessões), isto vazaria
    # RAM e cresceria a estrutura indefinidamente. ``OrderedDict`` com cap
    # de 256 evita o problema sem custar mais que O(1) por acesso.
    _SESSION_LOCKS: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
    _SESSION_LOCKS_MAX: int = 256
    # Loop-bound lock-guard for session-lock creation; see
    # :class:`LoopBoundLock` for the rebinding semantics.
    _SESSION_LOCKS_GUARD_HOLDER: LoopBoundLock = LoopBoundLock()

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
                                # Defense-in-depth (iter-2 review): rejeita
                                # campos extras antes de spread para
                                # ``SubAgentTask.__init__``.
                                "additionalProperties": False,
                            },
                        },
                    },
                },
                required=["subtasks"],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.SYSTEM,
                # iter-2 review: lê budget em runtime (respeita override por
                # env/settings) ao invés do snapshot-on-import legado.
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
            session_lock = await self._get_session_lock(session_id)
            async with session_lock:
                now = time.monotonic()
                self._prune_expired(now)
                if is_in_cooldown(
                    self._LAST_DISPATCH, session_id,
                    self._DISPATCH_COOLDOWN_S, now,
                ):
                    last = self._LAST_DISPATCH[session_id]
                    remaining = self._DISPATCH_COOLDOWN_S - (now - last)
                    return ToolResult.error_result(
                        f"dispatch_parallel_subagents já foi chamado há {now-last:.0f}s; "
                        f"aguarde {remaining:.1f}s ou explique ao usuário o resultado "
                        f"da chamada anterior. NÃO chame de novo esperando resultado "
                        f"diferente.",
                        error_code="DISPATCH_COOLDOWN",
                    )
                record_dispatch(self._LAST_DISPATCH, session_id, now)

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

            # MA6 / M12 (iter-2 review): emite audit ANTES + DEPOIS da
            # execução. ``result='accepted'`` é a fase de admissão (Pilar 8 —
            # AuditEvent.result deve refletir um estado tipado, não 'started'
            # solto). O evento terminal (success/failure/cancelled/budget) é
            # emitido no ``finally`` abaixo, capturando o desfecho real para
            # auditoria completa.
            audit_details = {
                "n_subtasks": len(tasks),
                "runner": runner.__class__.__name__,
                "max_parallel": max_parallel,
            }
            self._audit_emit(
                action="dispatch",
                result="accepted",
                session_id=session_id,
                details=audit_details,
            )

            # Eleva profundidade de nesting — sub-DEILEs spawnados via
            # asyncio.create_task herdam este ContextVar e bloqueiam recursão.
            # ``_nesting_depth_inc`` (helper contextmanager) garante reset
            # mesmo em paths de exceção.
            result = None
            audit_result = "failure"
            try:
                with _nesting_depth_inc():
                    result = await orchestrator.run(tasks)
                # Mapeia desfecho real para o vocabulário do AuditEvent.
                if result.cancelled:
                    audit_result = "cancelled"
                elif result.ok_global:
                    audit_result = "success"
                else:
                    audit_result = "failure"
            except asyncio.CancelledError:
                # MN3 (iter-3 review): quando o caller cancela esta corrotina
                # (parent_cancel via ESC ou worker shutdown), ``orchestrator.run``
                # propaga ``CancelledError`` em vez de retornar. Sem este handler,
                # o ``audit_result`` ficaria em ``'failure'`` (default), perdendo
                # fidelidade — o desfecho real é ``'cancelled'``. Pilar 03 §6:
                # CancelledError NUNCA é capturada sem re-raise; o ``finally``
                # abaixo emite o evento terminal com o vocabulário correto, e
                # então re-raise propaga até o caller.
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
                    # Discrimina budget_exceeded (subagent_budget_exceeded
                    # vira o ``error`` dos states cancelados pelo budget).
                    if any(
                        (st.error or "").strip() == "subagent_budget_exceeded"
                        for st in result.states
                    ):
                        audit_result = "budget_exceeded"
                self._audit_emit(
                    action="dispatch",
                    result=audit_result,
                    session_id=session_id,
                    details=terminal_details,
                )

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

    def _audit_emit(
        self,
        *,
        action: str,
        result: str,
        session_id: str,
        details: dict,
    ) -> None:
        """Emit a typed audit event for this tool (best-effort, never raises).

        Pilar 8 (segurança) — toda ação tool-driven precisa de trilha
        auditável. ``tool_name=self.name`` (Pilar 4 — Tools resolvem
        identidade pelo registry, sem string literal).

        NT4 (iter-3 review): ``actor`` é o papel/categoria (``'tool'``),
        e ``tool_name`` é a identidade específica via ``self.name``. Antes
        os dois carregavam o mesmo valor — redundância sem ganho de
        informação. Outros emissores no projeto seguem este padrão de
        ``actor`` como papel (``'system'``, ``'secrets_scanner'``,
        ``'plan_manager'``) distinto de ``tool_name``.
        """
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
                actor="tool",
                resource=f"session:{session_id}",
                action=action,
                result=result,
                details=details,
                tool_name=self.name,
            )
        except Exception:  # audit must never crash the tool
            # Item 14: degradation in the audit trail must not be invisible —
            # surface as a warning so operators notice missing trail entries.
            logger.warning("audit emission failed", exc_info=True)

    @classmethod
    def _prune_expired(cls, now: float) -> None:
        cutoff = cls._DISPATCH_COOLDOWN_S * 10
        prune_expired(cls._LAST_DISPATCH, cutoff, now)

    @classmethod
    def _get_locks_guard(cls) -> asyncio.Lock:
        """Lazy-init do lock-guarda por event loop (MA5 — iter-2 review).

        Delega a :class:`LoopBoundLock`, que cria/troca o Lock conforme o
        loop muda — evitando ``RuntimeError: ... is bound to a different
        event loop`` em múltiplos ``asyncio.run()`` (testes loop-per-test,
        CLI ``_run_self_install`` + ``_run_oneshot``).
        """
        return cls._SESSION_LOCKS_GUARD_HOLDER.get()

    @classmethod
    async def _get_session_lock(cls, session_id: str) -> asyncio.Lock:
        """Returns the per-session lock; LRU-bounded at ``_SESSION_LOCKS_MAX``.

        M5/M14 (PR #295 review): substitui o ``defaultdict(asyncio.Lock)``
        ilimitado por um ``OrderedDict`` com cap LRU — sessões fechadas que
        não voltarem a chamar a tool serão eventualmente desalojadas.
        Acesso é serializado por um lock-guarda para evitar race na criação
        sob disparos paralelos do mesmo session_id.

        MA1 (iter-2 review): a eviction não pode parar no primeiro lock-em-uso
        encontrado — antes a poda quebrava permanentemente quando o LRU-front
        estava ``locked()``. Agora itera por TODAS as entradas mais antigas
        que o cap, pulando as travadas (``continue``) e removendo as livres;
        se TODAS estiverem travadas, aceita overshoot temporário com um
        ``logger.warning``.

        MN1 (iter-3 review): NUNCA evict o ``session_id`` que acabou de ser
        criado/tocado. Edge case: se todas as entradas anteriores estiverem
        ``locked()``, o loop poderia chegar à recém-inserida e removê-la,
        permitindo que um caller concorrente para o MESMO session_id criasse
        um lock NOVO — quebrando o mutex per-sessão.
        """
        async with cls._get_locks_guard():
            lock = cls._SESSION_LOCKS.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                cls._SESSION_LOCKS[session_id] = lock
            else:
                # LRU: marca como mais recente.
                cls._SESSION_LOCKS.move_to_end(session_id)
            # Eviction: itera por toda a OrderedDict (oldest → newest) sem
            # break prematuro. Coleta candidatos não-locked, remove até cair
            # sob o cap. Se TUDO estiver locked, registra warning e segue.
            if len(cls._SESSION_LOCKS) > cls._SESSION_LOCKS_MAX:
                excess = len(cls._SESSION_LOCKS) - cls._SESSION_LOCKS_MAX
                removed = 0
                # snapshot dos ids ordenados (oldest first) para iterar sem
                # mutar durante o loop.
                ordered_ids = list(cls._SESSION_LOCKS.keys())
                for sid in ordered_ids:
                    if removed >= excess:
                        break
                    # MN1: nunca evict o session_id recém-inserido/tocado —
                    # outro caller concorrente para o mesmo sid criaria um
                    # lock NOVO, quebrando mutex per-sessão.
                    if sid == session_id:
                        continue
                    candidate = cls._SESSION_LOCKS.get(sid)
                    if candidate is None:
                        continue
                    if candidate.locked():
                        # Skip locked — não interrompe a varredura.
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
        # No good break — delegate to the shared truncate helper for the
        # ellipsis-aware cut (keeps the single-source-of-truth for the
        # ellipsis char/width).
        from deile.common.text_utils import truncate
        truncated = truncate(text, max_chars)
    else:
        truncated = text[:cut].rstrip()
    # Se ficou com code-fence aberta (número ímpar de ```), fecha.
    if truncated.count("```") % 2 == 1:
        truncated += "\n```"
    # Item 15 — known limitation: unmatched ``[`` (link), ``*``/``_``
    # (emphasis), or ``<`` (html-ish) sequences cut mid-token are NOT
    # rebalanced here. They can leak into the LLM's consolidation context
    # as syntactically-invalid markdown but the downstream renderer treats
    # them as literal text — not a prompt-injection vector in this
    # codebase because ``summary_for_llm`` is delivered as a tool-result
    # payload, not interpreted as new instructions. Full markdown
    # rebalancing would require a real tokenizer; intentionally deferred.
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
