"""Agent Orchestrator principal do DEILE"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from .exceptions import DEILEError, ModelError

# Module-level import so that `except _BudgetExceeded:` clauses don't NameError
# if the import inside a try block ever raises (extremely rare but possible).
try:
    from deile.storage.usage_repository import \
        BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover — defensive only
    class _BudgetExceeded(Exception):  # type: ignore[no-redef]
        provider_id = None
        limit_type = None
from ..commands.registry import get_command_registry
from ..config.settings import get_settings
from ..orchestration.workflow_executor import get_workflow_executor
from ..parsers.base import ParseResult
from ..parsers.registry import ParserRegistry, get_parser_registry
from ..personas.manager import PersonaManager
from ..storage.logs import get_logger
from ..tools.base import ToolContext, ToolResult, ToolStatus
from ..tools.registry import ToolRegistry, get_tool_registry
from ..ui.display_manager import DisplayManager
from . import validation_gate as _validation_gate
from .agent_autonomous import AgentAutonomousMixin
from .agent_streaming import AgentStreamingMixin
from .context_manager import ContextManager
from .intent_analyzer import get_intent_analyzer
from .models.router import ModelRouter
from .proactive_analyzer import (ProactiveAction, ProactiveAnalyzer,
                                 get_proactive_analyzer)

logger = logging.getLogger(__name__)


def _record_model_used(session: Any, provider: Any) -> None:
    """Store the provider:model key used by the current turn into session context."""
    session.context_data["_last_model_used"] = (
        f"{provider.provider_id}:{provider.model_name}"
    )


_PLAIN_CONSOLE: Any = None


def _normalize_history_content(content: Any) -> str:
    """Coerce arbitrary content into plain text suitable for replay.

    Slash commands and tools sometimes return Rich renderables (Panel,
    Table, Text). Those don't survive JSON serialization when the
    conversation history is replayed to the provider, so we render them
    to plain text once at write time and reuse a module-level Console
    via ``capture()`` instead of spinning up a fresh one per call.
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    global _PLAIN_CONSOLE
    try:
        if _PLAIN_CONSOLE is None:
            from rich.console import Console
            _PLAIN_CONSOLE = Console(
                force_terminal=False,
                no_color=True,
                width=120,
                highlight=False,
                soft_wrap=False,
            )
        with _PLAIN_CONSOLE.capture() as cap:
            _PLAIN_CONSOLE.print(content)
        return cap.get().strip()
    except Exception:
        return str(content)


def _open_turn_span(
    *,
    session_id: str,
    turn_number: int,
    persona: str,
    input_length: int,
) -> Tuple[Any, Any]:
    """Abre o span ``deile.turn`` manualmente (sem ``with``).

    Devolve ``(span_cm, span)`` — ambos ``None`` se observability falhou.
    Pareado com :func:`_finalize_turn_span` que fecha o CM no ``finally``.
    O span é aberto fora do ``with`` para não re-indentar o turno inteiro
    (mantém os ``async for`` e ``return`` no nível atual).
    """
    try:
        from deile.observability import get_tracer  # noqa: PLC0415
        span_cm = get_tracer().turn(
            session_id=str(session_id),
            turn_number=int(turn_number),
            persona=persona,
            model="",
            input_length=int(input_length),
        )
        return span_cm, span_cm.__enter__()
    except Exception:  # noqa: BLE001 — observability nunca quebra o turn
        return None, None


def _record_turn_error(span: Any, exc: BaseException, component: str) -> None:
    """Marca o span do turn como ERROR e incrementa o counter ``deile.errors.total``.

    Best-effort — observability nunca quebra o turn (princípio 11 + Fase 1).
    Usado pelo ``process_input``/``process_input_stream``: o span é criado
    fora do try, então qualquer except do turn passa por aqui antes do raise.
    """
    if span is not None:
        try:
            from opentelemetry.trace import Status, StatusCode  # noqa: PLC0415
            span.set_status(Status(StatusCode.ERROR, description=type(exc).__name__))
            span.record_exception(exc)
        except Exception:  # noqa: BLE001 — observability nunca quebra
            pass
    try:
        from deile.observability import get_metrics  # noqa: PLC0415
        get_metrics().record_error(type(exc).__name__, component)
    except Exception:  # noqa: BLE001
        pass


def _finalize_turn_span(span_cm: Any, duration_ms: int, persona: str) -> None:
    """Sai do CM do turn, registra a histogram de duração e ignora qualquer falha.

    Chamado no ``finally`` do turno — depois da resposta normal OU do except.
    """
    if span_cm is not None:
        try:
            span_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
    try:
        from deile.observability import get_metrics  # noqa: PLC0415
        get_metrics().record_turn_duration(persona=persona or "unknown", duration_ms=duration_ms)
    except Exception:  # noqa: BLE001
        pass


def _is_permanent_provider_error(exc: Exception) -> bool:
    """Return True for errors that retrying a different provider cannot fix.

    Permanent errors (auth, invalid_request/model_not_found) should bubble up to the
    user so they can correct their configuration instead of silently cascading.
    Transient errors (rate_limit, server) may be retried via cascade.
    """
    from deile.core.models.errors import \
        ProviderInvocationError  # noqa: PLC0415
    if not isinstance(exc, ProviderInvocationError):
        return False
    return exc.envelope.error_type in ("auth", "invalid_request", "context_length_exceeded")


def _coerce_model_handle(value: Any) -> Optional[str]:
    """Return a normalized provider:model_id handle, or None if malformed."""
    if not isinstance(value, str):
        return None
    handle = value.strip()
    if ":" not in handle:
        return None
    provider_id, model_id = handle.split(":", 1)
    provider_id = provider_id.strip()
    model_id = model_id.strip()
    if not provider_id or not model_id:
        return None
    return f"{provider_id}:{model_id}"


def _provider_for_handle(model_router: ModelRouter, handle: str) -> Optional[Any]:
    provider_id, model_id = handle.split(":", 1)
    for provider in model_router.providers.values():
        if (
            getattr(provider, "provider_id", None) == provider_id
            and getattr(provider, "model_name", None) == model_id
        ):
            return provider
    return None


def _available_models_for_provider(model_router: ModelRouter, provider_id: str) -> List[str]:
    return sorted({
        getattr(provider, "model_name", "?")
        for provider in model_router.providers.values()
        if getattr(provider, "provider_id", None) == provider_id
    })


def _get_config_default_model() -> Optional[str]:
    try:
        from deile.config.manager import get_config_manager

        return _coerce_model_handle(get_config_manager().get_config().default_model)
    except Exception:
        return None


def _select_configured_model_provider(
    model_router: ModelRouter,
    session: Any,
) -> Tuple[Optional[Any], Optional[str], Optional[str], Optional[str]]:
    """Resolve hard and soft model preferences.

    Precedence:
    1. session.context_data["forced_model"] — hard /model override. Missing
       registration is a user-visible error.
    2. session.context_data["preferred_model"] — soft integration preference.
    3. legacy session.context_data["_bot_forced_model"] — soft compatibility key.
    4. api_config.yaml default_model — existing core soft preference.

    Soft preferences are best-effort. If a handle is malformed or not registered,
    the caller falls through to the normal router instead of failing the turn.
    """
    context_data = getattr(session, "context_data", {}) or {}
    forced_raw = context_data.get("forced_model")
    forced = _coerce_model_handle(forced_raw)
    if forced_raw and forced is None:
        raise ModelError(
            f"Forced model '{forced_raw}' is invalid. Use provider:model_id.",
            error_code="FORCED_MODEL_NOT_REGISTERED",
        )
    if forced:
        provider = _provider_for_handle(model_router, forced)
        if provider is not None:
            return provider, forced, None, None
        provider_id = forced.split(":", 1)[0]
        available = _available_models_for_provider(model_router, provider_id)
        raise ModelError(
            f"Forced model '{forced}' is not registered. "
            f"Available {provider_id} models: {available or '(none)'}. "
            f"Use /model use auto to clear the override.",
            error_code="FORCED_MODEL_NOT_REGISTERED",
        )

    soft_candidates = [
        ("preferred_model", context_data.get("preferred_model")),
        ("_bot_forced_model", context_data.get("_bot_forced_model")),
        # Operator-set GLOBAL preference (env DEILE_PREFERRED_MODEL /
        # settings.json model.preferred). Soft: skipped when unset (default
        # None) or when the handle is not registered. This is what lets a
        # deployment pin its model (e.g. the deile-worker to
        # ``deepseek:deepseek-v4-pro``) without a hard ``/model`` lock — and it
        # makes the long-existing DEILE_PREFERRED_MODEL env actually take effect
        # (previously it was read into Settings but never consulted here).
        ("settings.preferred_model", get_settings().preferred_model),
        ("default_model", _get_config_default_model()),
    ]
    for source, raw_handle in soft_candidates:
        handle = _coerce_model_handle(raw_handle)
        if raw_handle and handle is None:
            logger.warning("Ignoring malformed %s value: %r", source, raw_handle)
            continue
        if not handle:
            continue
        provider = _provider_for_handle(model_router, handle)
        if provider is not None:
            return provider, None, handle, source
        logger.debug("%s '%s' not registered, falling back", source, handle)
    return None, None, None, None


def _self_record_circuit(provider_id: str, *, success: bool) -> None:
    """Notify TierRouter's CircuitBreaker of a provider call outcome."""
    try:
        from deile.core.models.tier_router import \
            get_tier_router  # noqa: PLC0415
        tr = get_tier_router()
        if success:
            tr.record_success(provider_id)
        else:
            tr.record_failure(provider_id)
    except Exception as exc:
        logger.debug("circuit-record failed for %s (success=%s): %s", provider_id, success, exc)


async def _emit_router_event(event_type: str, payload: dict) -> None:
    """Best-effort observability emit; never raises."""
    try:
        from deile.storage.debug_logger import \
            get_debug_logger  # noqa: PLC0415
        await get_debug_logger().log_router_event(event_type, payload)
    except Exception:
        pass


class AgentStatus(Enum):
    """Status do agente"""
    IDLE = "idle"
    PROCESSING = "processing"
    EXECUTING_TOOL = "executing_tool"
    GENERATING_RESPONSE = "generating_response"
    ERROR = "error"


@dataclass
class AgentSession:
    """Sessão do agente com estado persistente"""
    session_id: str
    user_id: Optional[str] = None
    working_directory: Path = field(default_factory=lambda: Path.cwd())
    context_data: Dict[str, Any] = field(default_factory=dict)
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    persisted: bool = False

    def update_activity(self) -> None:
        """Atualiza timestamp da última atividade"""
        self.last_activity = time.time()

    def snapshot(self) -> Dict[str, Any]:
        """Serialize session to a JSON-friendly dict (context_data + metadata)."""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "working_directory": str(self.working_directory),
            "context_data": dict(self.context_data),
            "created_at": self.created_at,
            "last_activity": self.last_activity,
        }

    @classmethod
    def from_snapshot(cls, snap: Dict[str, Any]) -> "AgentSession":
        """Rebuild session from a snapshot dict."""
        return cls(
            session_id=snap["session_id"],
            user_id=snap.get("user_id"),
            working_directory=Path(snap.get("working_directory") or Path.cwd()),
            context_data=dict(snap.get("context_data") or {}),
            created_at=float(snap.get("created_at") or time.time()),
            last_activity=float(snap.get("last_activity") or time.time()),
            persisted=True,
        )
    
    def get_context_value(self, key: str, default: Any = None) -> Any:
        """Obtém valor do contexto da sessão"""
        return self.context_data.get(key, default)
    
    def set_context_value(self, key: str, value: Any) -> None:
        """Define valor no contexto da sessão"""
        self.context_data[key] = value
    
    def add_to_history(self, role: str, content: Any, metadata: Optional[Dict] = None) -> None:
        """Adiciona entrada ao histórico da conversa.

        Coerces `content` to plain text. Slash-command handlers (and some tools)
        produce Rich renderables (Panel, Table, Text) as response content; the
        provider HTTP path JSON-serializes message content, so non-string
        objects must be normalized at the boundary or they break subsequent
        turns once the history is replayed to the model.
        """
        entry = {
            "role": role,
            "content": _normalize_history_content(content),
            "timestamp": time.time(),
            "metadata": metadata or {}
        }
        self.conversation_history.append(entry)


@dataclass
class AgentResponse:
    """Resposta do agente"""
    content: str
    status: AgentStatus
    tool_results: List[ToolResult] = field(default_factory=list)
    parse_result: Optional[ParseResult] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    execution_time: float = 0.0
    error: Optional[Exception] = None
    
    @property
    def is_success(self) -> bool:
        """Verifica se a resposta foi bem-sucedida"""
        return self.status != AgentStatus.ERROR and self.error is None
    
    @property
    def has_tool_results(self) -> bool:
        """Verifica se há resultados de tools"""
        return len(self.tool_results) > 0


class DeileAgent(AgentStreamingMixin, AgentAutonomousMixin):
    """Orquestrador principal do DEILE
    
    Coordena a interação entre parsers, tools, context manager e modelos de IA
    implementando o padrão Mediator para centralizar a lógica de orquestração.
    """
    
    def __init__(
        self,
        tool_registry: Optional[ToolRegistry] = None,
        parser_registry: Optional[ParserRegistry] = None,
        context_manager: Optional[ContextManager] = None,
        model_router: Optional[ModelRouter] = None,
        display_manager: Optional[DisplayManager] = None,
        config_manager = None
    ):
        self.config_manager = config_manager
        self.tool_registry = tool_registry or get_tool_registry()
        self.parser_registry = parser_registry or get_parser_registry()
        self.context_manager = context_manager or ContextManager()
        self.model_router = model_router or ModelRouter()
        
        # Initialize display system - requires Rich Console
        if display_manager:
            self.display_manager = display_manager
        else:
            from rich.console import Console
            console = Console()
            self.display_manager = DisplayManager(console)
        
        # Initialize command system
        self.command_registry = get_command_registry(config_manager)

        self.settings = get_settings()
        self.logger = get_logger()

        self._status = AgentStatus.IDLE
        self._sessions: Dict[str, AgentSession] = {}
        self._request_count = 0
        self._skill_loader = None  # set by _auto_discover_components; used by reload_skills()
        self._skills_watcher = None  # set by _auto_discover_components; stopped in shutdown()

        # Initialize PersonaManager - será inicializado via async initialize()
        self.persona_manager: Optional[PersonaManager] = None
        self.persona_enhanced: bool = False

        # Initialize WorkflowExecutor for TODO list management
        self.workflow_executor = None

        # Initialize IntentAnalyzer for intelligent workflow detection
        intent_config_path = Path(__file__).parent.parent / "config" / "intent_patterns.yaml"
        self.intent_analyzer = get_intent_analyzer(intent_config_path)

        # Initialize ProactiveAnalyzer for automatic tool execution
        self.proactive_analyzer: Optional[ProactiveAnalyzer] = None

        # Auto-discover tools, parsers, and commands
        self._auto_discover_components()

        # Surface UI opcional para tools que abrem renderers próprios
        # (issue #257: ``dispatch_parallel_subagents`` abre um painel Rich
        # Live multiplexado durante a execução). A CLI seta isso via
        # :meth:`set_ui_console` logo após construir o agente; quando o
        # agente roda fora de um CLI (e.g. worker pod), o atributo
        # permanece ``None`` e a tool roda em modo headless.
        self._ui_console = None

    def set_ui_console(self, console) -> None:
        """Registra o ``rich.console.Console`` da UI para tools que precisem dele.

        Issue #257 — a tool ``dispatch_parallel_subagents`` consulta isso
        via ``session.context_data["_console"]`` para abrir o painel
        multiplexado ao vivo. Setar ``None`` desabilita a UI (modo headless,
        e.g. testes).
        """
        self._ui_console = console

    async def initialize(self) -> None:
        """Inicializa componentes assíncronos do agente"""
        try:
            # Inicializa PersonaManager com integração de memória
            memory_manager = getattr(self, 'memory_manager', None)
            self.persona_manager = PersonaManager(memory_manager=memory_manager)
            await self.persona_manager.initialize()

            # Validate memory integration
            if memory_manager and hasattr(self.persona_manager, 'validate_memory_integration'):
                if self.persona_manager.validate_memory_integration():
                    logger.info("PersonaManager memory integration validated successfully")
                else:
                    logger.warning("PersonaManager memory integration validation failed")

            # Ativa persona padrão
            await self.persona_manager.switch_persona("developer")

            # Inicializa ProactiveAnalyzer
            self.proactive_analyzer = get_proactive_analyzer(str(self.settings.working_directory))

            # Configura integração profunda com persona systems
            await self._setup_persona_integration()

            self.persona_enhanced = True

            # Inicializa WorkflowExecutor
            self.workflow_executor = get_workflow_executor()

            logger.info("Agent initialized successfully with PersonaManager and WorkflowExecutor")

        except Exception as e:
            logger.error(f"Error initializing agent: {e}")
            # Continua funcionando sem PersonaManager se necessário
            logger.warning("Continuing without PersonaManager")

    @property
    def status(self) -> AgentStatus:
        """Status atual do agente"""
        return self._status
    
    @property
    def request_count(self) -> int:
        """Contador de requisições processadas"""
        return self._request_count
    
    async def process_input(
        self,
        user_input: str,
        session_id: str = "default",
        **kwargs
    ) -> AgentResponse:
        """Processa entrada do usuário através do pipeline completo
        
        Args:
            user_input: Entrada do usuário
            session_id: ID da sessão
            **kwargs: Parâmetros adicionais
            
        Returns:
            AgentResponse: Resposta processada do agente
        """
        start_time = time.time()
        self._status = AgentStatus.PROCESSING
        self._request_count += 1
        # Issue #303 — conta turno no runtime state (singleton, best-effort).
        try:
            from deile.runtime.instance_state import get_instance_state
            get_instance_state().update_stats(turns=1)
        except Exception:  # noqa: BLE001 — runtime state nunca pode quebrar a turn
            pass

        # Issue #303 fase 4 — span pai do turn (deile.turn). Aberto via helper
        # que faz ``__enter__`` manual (sem ``with``) para não re-indentar o
        # método inteiro; ``__exit__`` vem em ``_finalize_turn_span`` no finally.
        _turn_span_cm, _turn_span = _open_turn_span(
            session_id=session_id,
            turn_number=self._request_count,
            persona=str(self.current_persona) if getattr(self, "current_persona", None) else "",
            input_length=len(user_input or ""),
        )

        try:
            # Bot-hooks: extract optional kwargs added in plano DEILE fase 2.
            # These are stashed in session.context_data so context_manager and
            # tool dispatch can read them later in the turn.
            extra_system_prompt = kwargs.pop("extra_system_prompt", None)
            bot_context = kwargs.pop("bot_context", None)

            # Obtém ou cria sessão
            session = self._get_or_create_session(session_id, **kwargs)
            session.update_activity()

            if extra_system_prompt is not None:
                from deile.core.bot_hooks import sanitize_extra_system_prompt
                session.context_data["extra_system_prompt"] = (
                    sanitize_extra_system_prompt(str(extra_system_prompt))
                )
            if bot_context is not None:
                session.context_data["bot_context"] = dict(bot_context)

            # Intercepta comandos slash ANTES de processar — apenas se o comando existe.
            # Comandos desconhecidos (ex: /ideias-projetos) caem no LLM como linguagem natural.
            _stripped = user_input.strip()
            _is_known_slash = False
            if _stripped.startswith('/'):
                _cmd = _stripped[1:].split()[0] if _stripped[1:] else ""
                if _cmd and self.command_registry.has_command(_cmd):
                    _is_known_slash = True

            # Slash commands conhecidos NÃO entram no histórico user — são
            # comandos da CLI, não mensagens para o LLM. Sem essa exclusão,
            # ``/rewind``, ``/help``, etc. poluem o seletor de rewind e
            # contaminam o contexto do LLM nas próximas turns. Skills
            # (``llm_prompt``) re-adicionam com o body real do prompt em
            # ``_process_slash_command``.
            if not _is_known_slash:
                session.add_to_history("user", user_input)

            if _is_known_slash:
                return await self._process_slash_command(_stripped, session, start_time)
            
            # AUTONOMY PHASE: Try autonomous processing first
            autonomous_result = await self.process_autonomous_request(user_input, session)
            if autonomous_result:
                # If autonomous processing succeeded, return early
                response = AgentResponse(
                    content=autonomous_result,
                    status=AgentStatus.IDLE,
                    tool_results=[],
                    parse_result=None,
                    execution_time=time.time() - start_time
                )
                session.add_to_history("assistant", autonomous_result, {
                    "autonomous": True,
                    "execution_time": time.time() - start_time
                })
                logger.info(f"✅ Autonomous processing successful for: '{user_input[:50]}...'")
                return response

            # Fase 1: Parsing da entrada
            parse_result = await self._parse_input(user_input, session)

            # NOVA FUNCIONALIDADE: Execução proativa de ferramentas contextuais
            proactive_results = await self._execute_proactive_tools(user_input, session)

            # NOVA FUNCIONALIDADE: Detecta se precisa criar workflow automático
            workflow_needed = await self._should_create_workflow(user_input, parse_result)

            if workflow_needed and self.workflow_executor:
                # Cria e executa workflow automaticamente
                response_content, tool_results = await self._process_with_workflow(
                    user_input, parse_result, session
                )
            else:
                # Fase 2: Execução iterativa de tools e Function Calling
                response_content, tool_results = await self._process_iterative_function_calling(
                    user_input, parse_result, session
                )

            # Fase 2.5: deterministic validation gate (anti-hallucination + post-write).
            response_content, tool_results = await self._apply_validation_gate(
                user_input=user_input,
                parse_result=parse_result,
                session=session,
                content=response_content,
                tool_results=tool_results,
            )

            # Combina resultados proativos com resultados normais
            all_tool_results = proactive_results + tool_results

            # Cria resposta
            response = AgentResponse(
                content=response_content,
                status=AgentStatus.IDLE,
                tool_results=all_tool_results,
                parse_result=parse_result,
                execution_time=time.time() - start_time,
                metadata={"model_used": session.context_data.get("_last_model_used")},
            )
            
            # Adiciona resposta ao histórico
            _history_meta: Dict[str, Any] = {
                "tool_results": len(all_tool_results),
                "proactive_results": len(proactive_results),
                "parse_status": parse_result.status.value if parse_result else None,
                "function_calling_enabled": True,
            }
            _pending_rc = session.context_data.pop("_last_reasoning_content", None)
            if _pending_rc:
                _history_meta["reasoning_content"] = _pending_rc
            session.add_to_history("assistant", response_content, _history_meta)

            self._status = AgentStatus.IDLE
            return response

        except Exception as e:
            self._status = AgentStatus.ERROR
            # Issue #303 fase 4 — registra erro no span/metrics (best-effort).
            _record_turn_error(_turn_span, e, component="process_input")
            # BudgetExceeded gets a structured, user-actionable response (no stack-trace dump)
            if isinstance(e, _BudgetExceeded):
                friendly = (
                    f"Budget limit reached ({getattr(e, 'limit_type', 'unknown')}): {str(e)}\n"
                    f"Use /model budget to view limits, or wait for the next window."
                )
                self.logger.warning("BudgetExceeded surfaced to user: %s", e)
                return AgentResponse(
                    content=friendly,
                    status=AgentStatus.ERROR,
                    error=e,
                    metadata={
                        "budget_exceeded": True,
                        "provider_id": getattr(e, "provider_id", None),
                        "limit_type": getattr(e, "limit_type", None),
                    },
                    execution_time=time.time() - start_time,
                )
            # FORCED_MODEL_NOT_REGISTERED gets the same structured treatment
            if isinstance(e, ModelError) and getattr(e, "error_code", "") == "FORCED_MODEL_NOT_REGISTERED":
                self.logger.warning("Forced model not registered surfaced to user: %s", e)
                return AgentResponse(
                    content=str(e),
                    status=AgentStatus.ERROR,
                    error=e,
                    metadata={
                        "forced_model_not_registered": True,
                        "error_code": "FORCED_MODEL_NOT_REGISTERED",
                    },
                    execution_time=time.time() - start_time,
                )
            error_msg = f"Error processing input: {str(e)}"
            self.logger.error(error_msg, exc_info=True)

            return AgentResponse(
                content=error_msg,
                status=AgentStatus.ERROR,
                error=e,
                execution_time=time.time() - start_time
            )
        finally:
            # Issue #303 fase 4 — fecha o span do turn + métrica de duração.
            _finalize_turn_span(
                _turn_span_cm,
                duration_ms=int((time.time() - start_time) * 1000),
                persona=str(self.current_persona) if getattr(self, "current_persona", None) else "",
            )

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        """Obtém sessão por ID"""
        return self._sessions.get(session_id)
    
    def create_session(self, session_id: str, **kwargs) -> AgentSession:
        """Cria nova sessão com working directory normalizado"""
        if session_id in self._sessions:
            raise DEILEError(f"Session {session_id} already exists")
        
        # ROBUSTEZ: Normalização do working_directory
        raw_working_dir = kwargs.get("working_directory", Path.cwd())
        try:
            if isinstance(raw_working_dir, str):
                normalized_working_dir = Path(raw_working_dir).resolve()
            else:
                normalized_working_dir = Path(raw_working_dir).resolve()
            
            # Verifica se o diretório existe
            if not normalized_working_dir.exists():
                self.logger.warning(f"Working directory does not exist: {normalized_working_dir}, using current directory")
                normalized_working_dir = Path.cwd().resolve()
            
            # Verifica se é um diretório
            if not normalized_working_dir.is_dir():
                self.logger.warning(f"Working directory is not a directory: {normalized_working_dir}, using current directory")
                normalized_working_dir = Path.cwd().resolve()
                
        except Exception as e:
            self.logger.error(f"Error normalizing working directory: {e}, using current directory")
            normalized_working_dir = Path.cwd().resolve()
        
        self.logger.debug(f"Creating session {session_id} with working_directory: {normalized_working_dir}")
        
        session = AgentSession(
            session_id=session_id,
            user_id=kwargs.get("user_id"),
            working_directory=normalized_working_dir
        )
        self._sessions[session_id] = session
        return session
    
    def delete_session(self, session_id: str) -> bool:
        """Remove sessão"""
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False
    
    async def get_available_tools(self) -> List[Dict[str, Any]]:
        """Lista tools disponíveis"""
        tools = []
        for tool in self.tool_registry.list_enabled():
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "category": tool.category,
                "version": tool.version,
                "execution_count": tool.execution_count
            })
        return tools
    
    async def get_available_parsers(self) -> List[Dict[str, Any]]:
        """Lista parsers disponíveis"""
        parsers = []
        for parser in self.parser_registry.list_enabled():
            parsers.append({
                "name": parser.name,
                "description": parser.description,
                "priority": parser.priority,
                "version": parser.version,
                "patterns": parser.patterns
            })
        return parsers
    
    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do agente"""
        return {
            "status": self._status.value,
            "request_count": self._request_count,
            "active_sessions": len(self._sessions),
            "tools": self.tool_registry.get_stats(),
            "parsers": self.parser_registry.get_stats(),
            "context_manager": await self.context_manager.get_stats() if hasattr(self.context_manager, 'get_stats') else {},
            "model_router": await self.model_router.get_stats() if hasattr(self.model_router, 'get_stats') else {},
            "intent_analyzer": self.intent_analyzer.get_metrics() if hasattr(self.intent_analyzer, 'get_metrics') else {}
        }
    
    def clear_conversation_history(self) -> None:
        """Limpa o histórico de conversas do agente"""
        if hasattr(self.context_manager, 'clear'):
            self.context_manager.clear()
        
        # Limpar histórico de todas as sessões ativas
        for session in self._sessions.values():
            if hasattr(session, 'messages'):
                session.messages.clear()
            if hasattr(session, 'clear_history'):
                session.clear_history()
    
    async def get_or_create_session(
        self,
        session_id: str,
        working_directory: Optional[str] = None,
        *,
        persisted: bool = False,
    ) -> AgentSession:
        """Async helper that resurrects from SessionStore if `persisted=True`.

        Default `persisted=False` keeps CLI behavior identical (in-memory only).
        Bot adapters call with `persisted=True` so a session survives restart.
        """
        if session_id in self._sessions:
            return self._sessions[session_id]
        if persisted:
            try:
                store = await self.get_session_store()
                row = await store.get(session_id)
                if row is not None:
                    snap = {
                        "session_id": session_id,
                        "user_id": None,
                        "working_directory": row.working_directory,
                        "context_data": row.context_data,
                        "created_at": time.time(),
                        "last_activity": time.time(),
                    }
                    session = AgentSession.from_snapshot(snap)
                    self._sessions[session_id] = session
                    await store.touch(session_id)
                    return session
            except Exception:
                logger.warning(
                    "SessionStore lookup failed; creating in-memory session",
                    exc_info=True,
                )
        kwargs: Dict[str, Any] = {}
        if working_directory:
            kwargs["working_directory"] = working_directory
        session = self.create_session(session_id, **kwargs)
        session.persisted = persisted
        if persisted:
            try:
                store = await self.get_session_store()
                await store.upsert(
                    session_id,
                    str(session.working_directory),
                    dict(session.context_data),
                )
            except Exception:
                logger.warning("SessionStore upsert failed", exc_info=True)
        return session

    async def get_session_store(self):
        """Lazy SessionStore singleton — public accessor."""
        if not hasattr(self, "_session_store") or self._session_store is None:
            try:
                from deilebot.foundation.settings import get_bot_settings

                from deile.core.session_store import SessionStore

                bot_settings = get_bot_settings()
                path = bot_settings.foundation.sessions_sqlite_path
            except Exception:
                from pathlib import Path as _P

                from deile.core.session_store import SessionStore

                path = _P("./data/deile_sessions.sqlite")
            store = SessionStore(path)
            await store.init()
            self._session_store = store
        return self._session_store

    async def flush_persisted_sessions(self) -> int:
        """Persist all sessions marked `persisted=True`. Returns count flushed."""
        if not hasattr(self, "_session_store") or self._session_store is None:
            return 0
        flushed = 0
        for sid, session in self._sessions.items():
            if not getattr(session, "persisted", False):
                continue
            try:
                await self._session_store.upsert(
                    sid,
                    str(session.working_directory),
                    dict(session.context_data),
                )
                flushed += 1
            except Exception:
                logger.warning(f"flush failed for session {sid}", exc_info=True)
        return flushed

    async def shutdown(self) -> None:
        """Graceful shutdown — flush sessions, close store."""
        try:
            await self.flush_persisted_sessions()
        except Exception:
            pass
        store = getattr(self, "_session_store", None)
        if store is not None:
            try:
                await store.close()
            except Exception:
                pass
            self._session_store = None
        watcher = getattr(self, "_skills_watcher", None)
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                pass
            self._skills_watcher = None

    # Métodos privados

    def _get_or_create_session(self, session_id: str, **kwargs) -> AgentSession:
        """Obtém sessão existente ou cria nova.

        Sempre injeta a referência ao próprio agente (``_agent``) e ao
        console de UI (``_console``, opcional) no ``context_data`` da sessão
        para tools que precisem desses handles — por exemplo
        ``dispatch_parallel_subagents`` (issue #257), que spawna sub-DEILEs
        in-process e abre seu próprio Rich Live multipanel.
        """
        if session_id not in self._sessions:
            session = self.create_session(session_id, **kwargs)
        else:
            session = self._sessions[session_id]
        # Re-injection em cada turn é barata (apenas escreve no dict) e
        # sobrevive a substituições de console em testes. ``getattr`` defensivo
        # pq fixtures de teste constroem o agente via ``__new__`` (skip __init__).
        session.context_data["_agent"] = self
        _console = getattr(self, "_ui_console", None)
        if _console is not None:
            session.context_data["_console"] = _console
        session.context_data["session_id"] = session_id
        return session
    
    async def _process_slash_command(self, user_input: str, session: AgentSession, start_time: float) -> AgentResponse:
        """Processa comando slash diretamente"""
        try:
            # Parse comando slash
            parts = user_input[1:].split(' ', 1)  # Remove '/' e separa comando dos args
            command_name = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            
            # Cria contexto do comando
            from ..commands.base import CommandContext
            context = CommandContext(
                user_input=user_input,
                args=args,
                session_id=session.session_id,
                working_directory=str(session.working_directory)
            )
            # Injeta referências adicionais
            context.agent = self
            context.config_manager = self.config_manager
            context.session = session
            
            # Executa comando
            command_result = await self.command_registry.execute_command(command_name, context)

            # Skills (and future LLM commands) return content_type="llm_prompt".
            # Route the prompt through the LLM pipeline instead of returning it raw.
            if command_result.content_type == "llm_prompt" and command_result.content:
                prompt = str(command_result.content)
                # ``process_input`` agora NÃO adiciona slash commands ao histórico
                # (são comandos da CLI). Para skills, o body real do prompt é
                # o que o LLM deve ver — adiciona aqui como entrada user
                # legítima (substitui a antiga lógica de "varrer reversed
                # history e mutar entry[content]" que dependia da entrada
                # slash já existir).
                session.add_to_history("user", prompt)
                response_content, tool_results = await self._process_iterative_function_calling(
                    prompt, None, session
                )
                response = AgentResponse(
                    content=response_content,
                    status=AgentStatus.IDLE,
                    tool_results=tool_results,
                    parse_result=None,
                    execution_time=time.time() - start_time,
                    metadata={
                        "command_executed": command_name,
                        "command_status": command_result.status.value,
                        "is_slash_command": True,
                    },
                )
                session.add_to_history("assistant", response_content, {
                    "command": command_name,
                    "command_status": command_result.status.value,
                })
                return response

            # Converte resultado do comando para AgentResponse. O metadata
            # do CommandResult é mesclado primeiro para que flags como
            # ``suppress_response_display`` (escritas por /clear, /resume)
            # cheguem ao loop da CLI; chaves fixas do agente sobrescrevem
            # caso colidam.
            merged_meta: Dict[str, Any] = dict(command_result.metadata or {})
            merged_meta.update({
                "command_executed": command_name,
                "command_status": command_result.status.value,
                "is_slash_command": True,
            })
            response = AgentResponse(
                content=command_result.content or f"Command /{command_name} executed",
                status=AgentStatus.IDLE,
                tool_results=[],
                parse_result=None,
                execution_time=time.time() - start_time,
                metadata=merged_meta,
            )

            # Adiciona resposta ao histórico
            session.add_to_history("assistant", response.content, {
                "command": command_name,
                "command_status": command_result.status.value
            })

            return response

        except Exception as e:
            self.logger.error(f"Error processing slash command: {e}")
            return AgentResponse(
                content=f"Error executing command: {str(e)}",
                status=AgentStatus.ERROR,
                tool_results=[],
                parse_result=None,
                execution_time=time.time() - start_time
            )
    
    async def _parse_input(self, user_input: str, session: AgentSession) -> Optional[ParseResult]:
        """Fase 1: Parsing da entrada do usuário"""
        try:
            # Passa working_directory para parsers que precisam
            result = await self.parser_registry.parse(
                user_input, 
                working_directory=str(session.working_directory)
            )
            # self.logger.debug(f"Parse result: {result.status.value}")
            return result
        except Exception as e:
            self.logger.warning(f"Parsing failed: {e}")
            return None
    
    async def _execute_tools(
        self,
        parse_result: Optional[ParseResult],
        session: AgentSession
    ) -> List[ToolResult]:
        """Fase 2: Execução de tools baseada no parsing"""
        if not parse_result or not parse_result.tool_requests:
            return []

        self._status = AgentStatus.EXECUTING_TOOL
        tool_results = []
        # Issue #303 — singleton de runtime state; publish_action é best-effort.
        from deile.runtime.instance_state import get_instance_state
        _istate = get_instance_state()

        for tool_name in parse_result.tool_requests:
            try:
                # Cria contexto para a tool. Bot mode propaga bot_context para
                # ctx.extra (D4 plano DEILE) — tools que precisam leem dali.
                _bot_ctx = session.context_data.get("bot_context") or {}
                context = ToolContext(
                    user_input=session.conversation_history[-1]["content"] if session.conversation_history else "",
                    parsed_args=parse_result.commands[0].arguments if parse_result.commands else {},
                    session_data=session.context_data,
                    working_directory=str(session.working_directory),
                    file_list=parse_result.file_references,
                    extra={"bot_context": dict(_bot_ctx)} if _bot_ctx else {},
                )

                # Issue #303 — publica intenção; tool_name é safe (não args).
                try:
                    _istate.update_action(
                        "tool_execution",
                        detail=tool_name,
                        session_id=session.session_id,
                    )
                except Exception:  # noqa: BLE001 — runtime state é observability
                    pass

                # Executa a tool
                result = await self.tool_registry.execute_tool(tool_name, context)
                tool_results.append(result)

                # Display tool result using DisplayManager - SOLVES SITUAÇÃO 2 & 3
                self.display_manager.display_tool_result(tool_name, result)

                try:
                    _istate.update_stats(tool_calls=1)
                except Exception:  # noqa: BLE001
                    pass

                # self.logger.info(f"Tool {tool_name} executed: {result.status.value}")

            except Exception as e:
                error_result = ToolResult(
                    status=ToolStatus.ERROR,
                    error=e,
                    message=f"Failed to execute tool {tool_name}: {str(e)}"
                )
                tool_results.append(error_result)
                self.logger.error(f"Tool execution failed: {e}")
                try:
                    _istate.update_stats(errors=1)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                try:
                    _istate.clear_action()
                except Exception:  # noqa: BLE001
                    pass

        return tool_results
    
    # Non-streaming tool-loop. Still active when the streaming path bails out
    # or when a caller explicitly requests non-streaming behavior. All providers
    # registered via ``bootstrap_providers()`` implement either ``chat_with_tools``
    # (Anthropic, OpenAI, DeepSeek, Gemini) or the Gemini chat-session pair, so
    # providers without either path raise ``ModelError`` explicitly.
    async def _process_iterative_function_calling(
        self,
        user_input: str,
        parse_result: Optional[ParseResult],
        session: AgentSession
    ) -> tuple[str, List[ToolResult]]:
        """Processa function calling — Gemini via Chat Session; outros providers via chat_with_tools."""
        self._status = AgentStatus.GENERATING_RESPONSE

        try:
            # Prepara contexto inicial
            context = await self.context_manager.build_context(
                user_input=user_input,
                parse_result=parse_result,
                tool_results=[],
                session=session
            )

            # Classify intent tier and store in session for routing hints
            model_tier: Optional[Any] = None
            try:
                from deile.core.intent_tier_mapper import classify_tier
                intent_result = await self.intent_analyzer.analyze(
                    user_input=user_input,
                    parse_result=parse_result,
                    session_context={},
                )
                model_tier = classify_tier(intent_result)
                session.context_data["_current_tier"] = model_tier.value
            except Exception:
                model_tier = None

            # Resolve provider preference. /model use is hard; bot/default model
            # settings are soft and fall through to the router when unavailable.
            (
                model_provider,
                forced,
                selected_preference_handle,
                selected_preference_source,
            ) = _select_configured_model_provider(self.model_router, session)
            if model_provider is None:
                model_provider = await self.model_router.select_provider(
                    context=context,
                    session=session,
                    tier=model_tier,
                )

            # Budget enforcement — BudgetExceeded must propagate; other errors fail open
            try:
                from deile.storage.usage_repository import (
                    BudgetExceeded, BudgetGuard, get_usage_repository)
                _guard: Any = getattr(self, "_budget_guard_singleton", None)
                if _guard is None:
                    # YAML lives at deile/config/model_providers.yaml; agent.py is at deile/core/agent.py
                    _yaml = Path(__file__).resolve().parents[1] / "config" / "model_providers.yaml"
                    try:
                        self._budget_guard_singleton = BudgetGuard.from_yaml(_yaml, get_usage_repository())
                        _guard = self._budget_guard_singleton
                    except Exception as _init_err:
                        logger.debug("BudgetGuard init failed (%s) — checks disabled this session", _init_err)
                        self._budget_guard_singleton = False  # sentinel: tried and failed
                if _guard:
                    _guard.check_all(
                        session_id=session.session_id,
                        provider_id=model_provider.provider_id,
                    )
            except BudgetExceeded:
                raise  # propagate to caller; caller decides what to surface to user
            except Exception as _budget_err:
                logger.debug("BudgetGuard non-fatal: %s", _budget_err)

            # Observability: log provider selection
            await _emit_router_event(
                "provider_selected",
                {
                    "provider_id": model_provider.provider_id,
                    "model_id": getattr(model_provider, "model_name", "unknown"),
                    "tier": getattr(model_tier, "value", "unknown"),
                    "session_id": session.session_id,
                },
            )

            _t0 = time.time()

            # ── Gemini path: Chat Session with _gemini_chat_with_tools ──────────
            if hasattr(model_provider, 'create_chat_session') and hasattr(
                model_provider, '_gemini_chat_with_tools'
            ):
                logger.debug("Using Chat Session with manual function calling (Gemini)")

                system_instruction = None
                if isinstance(context, dict):
                    system_instruction = context.get("system_instruction")

                chat = await model_provider.create_chat_session(
                    session_id=session.session_id,
                    system_instruction=system_instruction
                )

                message_content: Any = user_input
                if isinstance(context, dict) and "file_data_parts" in context:
                    from .models.gemini_provider import GeminiProvider
                    message_parts: List[Any] = [user_input]
                    for file_data in context["file_data_parts"]:
                        if "file_data" in file_data:
                            file_uri = file_data["file_data"]["file_uri"]
                            file_obj = GeminiProvider.build_file_attachment_part(
                                file_uri=file_uri,
                                mime_type=file_data["file_data"].get("mime_type", "text/plain"),
                            )
                            message_parts.append(file_obj)
                    message_content = message_parts
                    logger.info("Sent message with %d file attachments", len(context["file_data_parts"]))

                try:
                    # `_gemini_chat_with_tools` agora devolve 3-tuple:
                    # (text, tool_results, ModelUsage agregado). Antes desse
                    # fix, retornava só 2-tuple e o usage era descartado —
                    # gemini nunca registrava nada na DB de custos.
                    content, tool_results, _gemini_usage = await model_provider._gemini_chat_with_tools(
                        chat=chat,
                        message=message_content,
                        working_directory=str(session.working_directory),
                        session_data=session.context_data,
                    )
                    # Persiste o usage agregado — sem isso, o caminho legado
                    # (que ainda é o default no agent) seguiria sem registrar
                    # nada mesmo com o helper preenchendo os tokens corretos.
                    try:
                        latency_ms = int((time.time() - _t0) * 1000)
                        await model_provider._record_usage(
                            session_id=str(session.session_id),
                            usage=_gemini_usage,
                            latency_ms=latency_ms,
                            success=True,
                        )
                    except Exception as _rec_err:  # noqa: BLE001 — telemetry must never block
                        logger.debug(
                            "gemini usage record (agent path) failed: %s",
                            _rec_err,
                        )
                    _self_record_circuit(model_provider.provider_id, success=True)
                except Exception as _gemini_err:
                    _self_record_circuit(model_provider.provider_id, success=False)
                    await _emit_router_event(
                        "cascade_fallback",
                        {
                            "provider_id": model_provider.provider_id,
                            "error": str(_gemini_err),
                            "latency_ms": int((time.time() - _t0) * 1000),
                        },
                    )
                    raise

                # Observability — completion event for Gemini path too
                await _emit_router_event(
                    "provider_call_completed",
                    {
                        "provider_id": model_provider.provider_id,
                        "tool_calls": len(tool_results),
                        "latency_ms": int((time.time() - _t0) * 1000),
                    },
                )

                logger.info("Chat session completed with %d tool execution(s)", len(tool_results))
                _record_model_used(session, model_provider)
                return content, tool_results

            # ── New providers: Anthropic / OpenAI / DeepSeek via chat_with_tools ─
            elif hasattr(model_provider, 'chat_with_tools'):
                from deile.core.models.base import \
                    ModelMessage  # local import to avoid cycle
                logger.debug("Using chat_with_tools for provider=%s", model_provider.provider_id)

                system_instruction = None
                raw_messages: List[Any] = []
                if isinstance(context, dict):
                    system_instruction = context.get("system_instruction")
                    raw_messages = context.get("messages", [])

                # Convert plain dict messages to ModelMessage objects (providers expect attrs, not keys)
                messages_for_provider: List[ModelMessage] = []
                for m in raw_messages:
                    if isinstance(m, ModelMessage):
                        messages_for_provider.append(m)
                    elif isinstance(m, dict):
                        role = str(m.get("role", "user"))
                        content_raw = m.get("content", "")
                        # Preserve string-typed content; for non-string (list/dict for multi-modal),
                        # keep the original object so providers that support structured content can handle it.
                        # ModelMessage.content is typed as str, but providers may pass it through as-is.
                        if isinstance(content_raw, str):
                            content = content_raw
                        else:
                            content = content_raw  # type: ignore[assignment]
                        msg_metadata = m.get("metadata", {}) or {}
                        messages_for_provider.append(
                            ModelMessage(role=role, content=content, metadata=msg_metadata)  # type: ignore[arg-type]
                        )
                # If context produced no messages, fall back to the raw user_input
                if not messages_for_provider:
                    messages_for_provider = [ModelMessage(role="user", content=user_input)]

                # Get ToolSchema objects from registered, enabled tools
                tools = [
                    t.schema for t in get_tool_registry().list_enabled() if getattr(t, "schema", None) is not None
                ]

                # Cascade retry loop: on provider failure, mark CB and try next tier provider.
                # We pass `skip_provider_ids` to TierRouter.select so a single in-request
                # failure short-circuits the cascade even before the CB threshold trips.
                # Cap is the cascade length so we never silently truncate longer cascades.
                MAX_CASCADE_ATTEMPTS = 3  # safe default for tier=None (no cascade) path
                if model_tier is not None:
                    try:
                        from deile.core.models.tier_router import \
                            get_tier_router as _gtr_init
                        cascade_len = len(_gtr_init().policy().cascade_for_tier(model_tier))
                        if cascade_len > 0:
                            MAX_CASCADE_ATTEMPTS = max(cascade_len, 1)
                    except Exception:
                        pass
                attempt = 0
                last_error: Optional[Exception] = None
                # tried_providers tracks failed provider_ids (not model_ids). A 401/auth or
                # network error on anthropic:opus implies anthropic:haiku will fail the same
                # way — so we skip the entire provider, not just the failing model.
                tried_providers: set = set()
                content = ""
                tool_results_raw: List[Any] = []
                while attempt < MAX_CASCADE_ATTEMPTS:
                    attempt += 1
                    tried_providers.add(model_provider.provider_id)
                    try:
                        # Provider records its own usage internally via _record_usage()
                        content, tool_results_raw, _usage = await model_provider.chat_with_tools(
                            messages=messages_for_provider,
                            tools=tools,
                            system_instruction=system_instruction,
                            session_id=session.session_id,
                        )
                        _self_record_circuit(model_provider.provider_id, success=True)
                        last_error = None
                        break
                    except Exception as _chat_err:
                        last_error = _chat_err
                        _self_record_circuit(model_provider.provider_id, success=False)
                        await _emit_router_event(
                            "cascade_fallback",
                            {
                                "provider_id": model_provider.provider_id,
                                "attempt": attempt,
                                "error": str(_chat_err),
                                "latency_ms": int((time.time() - _t0) * 1000),
                            },
                        )
                        # No retry path when tier wasn't classified or user explicitly forced
                        # this exact provider — the user chose it, we should not silently swap.
                        if model_tier is None or forced:
                            raise
                        # Permanent errors (auth / model-not-found) for a user-configured
                        # default_model must bubble up: cascading to another provider masks
                        # a misconfiguration the user needs to fix.
                        if (
                            selected_preference_source == "default_model"
                            and _is_permanent_provider_error(_chat_err)
                        ):
                            raise
                        try:
                            from deile.core.models.tier_router import \
                                get_tier_router as _gtr
                            next_provider = _gtr().select(
                                model_tier, skip_provider_ids=tried_providers
                            )
                        except Exception:
                            raise _chat_err  # cascade exhausted

                        model_provider = next_provider
                        logger.warning(
                            "%s '%s' failed (%s) — cascading to %s (attempt=%d)",
                            selected_preference_source or "router",
                            selected_preference_handle or "auto",
                            str(_chat_err)[:120],
                            model_provider.provider_id,
                            attempt,
                        )
                        # Re-run budget check against the new provider — daily/monthly
                        # limits are per-provider, not session-wide.
                        _guard_local: Any = getattr(self, "_budget_guard_singleton", None)
                        if _guard_local:
                            # _BudgetExceeded is module-level; if check_all raises it, propagate.
                            _guard_local.check_all(
                                session_id=session.session_id,
                                provider_id=model_provider.provider_id,
                            )
                        continue
                if last_error is not None:
                    raise last_error

                # Note: providers persist usage internally via _record_usage(); we do NOT re-record here.
                latency_ms = int((time.time() - _t0) * 1000)

                # Observability — completion event
                await _emit_router_event(
                    "provider_call_completed",
                    {
                        "provider_id": model_provider.provider_id,
                        "tool_calls": len(tool_results_raw),
                        "latency_ms": latency_ms,
                    },
                )

                tool_results: List[ToolResult] = [
                    tr for tr in tool_results_raw if isinstance(tr, ToolResult)
                ]
                logger.info(
                    "chat_with_tools completed (%s): %d tool call(s)",
                    model_provider.provider_id,
                    len(tool_results),
                )
                _record_model_used(session, model_provider)
                _rc = _usage.extra.get("reasoning_content")
                if _rc:
                    session.context_data["_last_reasoning_content"] = _rc
                return content, tool_results

            else:
                # Defensive — unreachable by construction: every provider registered via
                # bootstrap_providers() (Anthropic/OpenAI/DeepSeek/Gemini) inherits
                # ``BaseModelProvider.chat_with_tools``, so the ``elif hasattr(..., 'chat_with_tools')``
                # branch above always matches. This ``else`` only fires if a custom provider
                # explicitly overrides hasattr() to return False, which is not a supported pattern.
                provider_id = getattr(model_provider, "provider_id", type(model_provider).__name__)
                model_name = getattr(model_provider, "model_name", None) or getattr(
                    model_provider, "model_id", "unknown"
                )
                logger.error(
                    "Provider %s (model=%s) implements neither chat_with_tools nor the Gemini "
                    "chat-session pair — bootstrap_providers() should never register such a provider",
                    provider_id,
                    model_name,
                )
                raise ModelError(
                    f"Provider '{provider_id}' (model={model_name}) does not support function calling.",
                    error_code="PROVIDER_NO_TOOL_SUPPORT",
                )

        except Exception as e:
            # BudgetExceeded and structured ModelErrors must reach the caller — they carry
            # context the CLI uses to render Rich panels. _BudgetExceeded is module-level (line 18).
            if isinstance(e, _BudgetExceeded):
                # Emit observability event then propagate
                await _emit_router_event(
                    "budget_exceeded",
                    {
                        "session_id": session.session_id,
                        "provider_id": getattr(e, "provider_id", "unknown"),
                        "limit_type": getattr(e, "limit_type", "unknown"),
                        "message": str(e),
                    },
                )
                raise
            # Structured ModelErrors that the CLI renders as Rich panels must propagate.
            # FORCED_MODEL_NOT_REGISTERED: user-forced model is missing from the registry.
            # PROVIDER_NO_TOOL_SUPPORT: defensive — bootstrap_providers() should never register
            # a provider lacking both chat_with_tools and the Gemini session pair, but if it does
            # we want process_input to surface the misconfiguration instead of swallowing it.
            if isinstance(e, ModelError) and getattr(e, "error_code", "") in (
                "FORCED_MODEL_NOT_REGISTERED",
                "PROVIDER_NO_TOOL_SUPPORT",
            ):
                raise
            from deile.core.models.errors import ProviderInvocationError
            if isinstance(e, ProviderInvocationError) and e.envelope.is_context_length_exceeded:
                self.logger.warning("Context length exceeded for model %s", e.envelope.model_id)
                return (
                    f"O histórico desta conversa excedeu o limite de contexto do modelo **{e.envelope.model_id}**.\n\n"
                    "**Como resolver:**\n"
                    "• `/clear` — limpa o histórico e inicia uma nova sessão\n"
                    "• `/model select` — escolha um modelo com janela de contexto maior\n"
                    "• Divida sua pergunta em partes menores e mais objetivas\n"
                ), []
            self.logger.error(f"Chat session function calling failed: {e}", exc_info=True)
            return f"I encountered an error during function calling: {str(e)}", []

    # ------------------------------------------------------------------
    # Validation gate (anti-hallucination + post-write enforcement)
    # ------------------------------------------------------------------
    #
    # The implementation lives in ``deile.core.validation_gate`` (SRP / god-
    # object refactor). The methods below are thin wrappers preserving the
    # observable API used by the test suite and by ``self.…`` call sites
    # inside the streaming/legacy code paths.

    @staticmethod
    def _contains_promise_pattern(text: str) -> bool:
        """Delegate to ``validation_gate.contains_promise_pattern``."""
        return _validation_gate.contains_promise_pattern(text)

    @staticmethod
    def _detect_unvalidated_writes(
        tool_results: List[ToolResult],
    ) -> List[ToolResult]:
        """Delegate to ``validation_gate.detect_unvalidated_writes``."""
        return _validation_gate.detect_unvalidated_writes(tool_results)

    async def _apply_validation_gate(
        self,
        *,
        user_input: str,
        parse_result: Optional[ParseResult],
        session: AgentSession,
        content: str,
        tool_results: List[ToolResult],
    ) -> tuple[str, List[ToolResult]]:
        """Delegate to ``validation_gate.apply_validation_gate``.

        The retry callback is bound to ``self._process_iterative_function_calling``
        so the module never has to import ``DeileAgent`` (circular-import
        hazard).
        """
        return await _validation_gate.apply_validation_gate(
            user_input=user_input,
            parse_result=parse_result,
            session=session,
            content=content,
            tool_results=tool_results,
            retry=self._process_iterative_function_calling,
        )


    async def _should_create_workflow(self, user_input: str, parse_result: Optional[ParseResult]) -> bool:
        """Determina se deve criar workflow automaticamente usando análise de intenção avançada

        Esta versão refatorada usa o sistema IntentAnalyzer que implementa:
        - Análise léxica com word boundaries (evita falsos positivos)
        - Análise sintática com padrões regex otimizados
        - Análise semântica com embeddings
        - Sistema de confiança probabilística
        - Cache e métricas de performance

        **Degradação em falha:** se `IntentAnalyzer.analyze()` lançar exceção
        (embedding offline, cache corrompido, etc.), este método retorna `False`
        (no-workflow) por design. Esta é a opção #2 documentada na issue #308:
        `IntentAnalyzer` é a única árvore de decisão para essa pergunta — manter
        um fallback heurístico paralelo (legacy keyword-matching) violava SSOT e
        divergia ao longo do tempo. Como workflows são caros (multi-step +
        aprovação), o default conservador "não criar workflow" é mais seguro
        que adivinhar. NÃO reintroduza fallback heurístico sem revisitar #308.
        """
        try:
            # Prepara contexto da sessão para análise mais precisa
            session_context = await self._prepare_session_context_for_intent_analysis()

            # Executa análise de intenção multi-camada
            intent_result = await self.intent_analyzer.analyze(
                user_input=user_input,
                parse_result=parse_result,
                session_context=session_context
            )

            # Log da análise para debugging e métricas
            logger.debug(f"Intent analysis result: {intent_result}")
            logger.debug(f"Matched patterns: {intent_result.detected_patterns}")
            logger.debug(f"Matched keywords: {intent_result.matched_keywords}")

            # Determina se requer workflow baseado em thresholds OTIMIZADOS 2025
            # Passa configurações globais do analisador para thresholds dinâmicos
            requires_workflow = intent_result.requires_workflow(
                confidence_threshold=0.50,  # Reduzido para ser mais inclusivo
                complexity_threshold=0.35,  # Reduzido para detectar mais casos
                global_settings=getattr(self.intent_analyzer, 'global_settings', {})
            )

            # Log da decisão final
            if requires_workflow:
                logger.info(
                    f"Workflow creation triggered - Intent: {intent_result.intent_type.value}, "
                    f"Confidence: {intent_result.confidence:.2f}, "
                    f"Complexity: {intent_result.complexity_score:.2f}"
                )
            else:
                logger.debug(
                    f"Standard processing selected - Intent: {intent_result.intent_type.value}, "
                    f"Confidence: {intent_result.confidence:.2f}, "
                    f"Complexity: {intent_result.complexity_score:.2f}"
                )

            return requires_workflow

        except Exception as e:
            logger.error(
                "Intent analysis failed for workflow detection: %s — defaulting to no-workflow",
                e,
                exc_info=True,
            )
            return False

    async def _prepare_session_context_for_intent_analysis(self) -> Dict[str, Any]:
        """Prepara contexto da sessão para análise de intenção mais precisa"""
        try:
            # Obtém sessão ativa (assume sessão default se não especificada)
            session = self._sessions.get("default")

            if not session:
                return {}

            # Prepara dados contextuais
            context = {
                'conversation_length': len(session.conversation_history),
                'previous_tool_usage': sum(
                    1 for entry in session.conversation_history[-5:]  # últimas 5 entradas
                    if entry.get('metadata', {}).get('tool_results', 0) > 0
                ),
                'session_age': time.time() - session.created_at,
                'working_directory': str(session.working_directory),
                'recent_topics': self._extract_recent_topics(session.conversation_history[-3:])
            }

            return context

        except Exception as e:
            logger.warning(f"Error preparing session context for intent analysis: {e}")
            return {}

    def _extract_recent_topics(self, recent_history: List[Dict]) -> List[str]:
        """Extrai tópicos recentes do histórico para contexto"""
        topics = []

        for entry in recent_history:
            content = entry.get('content', '').lower()

            # Extrai palavras-chave técnicas simples
            technical_words = re.findall(r'\b(?:function|class|method|api|database|file|code|system|error|bug|feature|implement|create|analyze|fix|debug)\b', content)
            topics.extend(technical_words)

        # Remove duplicatas mantendo ordem
        return list(dict.fromkeys(topics))

    async def _process_with_workflow(
        self,
        user_input: str,
        parse_result: Optional[ParseResult],
        session: AgentSession
    ) -> tuple[str, List[ToolResult]]:
        """Processa solicitação criando workflow automático"""

        try:
            logger.info("Creating automatic workflow for user request")

            # Prepara contexto para workflow
            context = {
                'user_input': user_input,
                'session_id': session.session_id,
                'working_directory': str(session.working_directory)
            }

            # Se há parse result, usa informações dele
            if parse_result:
                context['tool_requests'] = parse_result.tool_requests
                context['file_references'] = parse_result.file_references
                if parse_result.commands:
                    context['parsed_commands'] = [cmd.action for cmd in parse_result.commands]

            # Cria e inicia workflow
            workflow_result = await self.workflow_executor.start_workflow_execution(
                objective=user_input,
                context=context
            )

            workflow_id = workflow_result['workflow_id']

            # Aguarda conclusão do workflow
            completion_result = await self.workflow_executor.wait_for_workflow_completion(
                workflow_id=workflow_id,
                timeout=timedelta(minutes=10)  # Timeout de 10 minutos
            )

            # Prepara resposta baseada no resultado
            if completion_result['success']:
                response_content = "✅ **Workflow concluído com sucesso!**\n\n"
                response_content += f"**Objetivo:** {user_input}\n"
                response_content += f"**Total de etapas:** {workflow_result['total_steps']}\n"
                response_content += f"**Status:** {completion_result['status']}\n\n"
                response_content += "Todos os passos foram executados sequencialmente e validados com sucesso."

                # Cria tool results sintéticos para compatibilidade
                tool_results = [ToolResult(
                    status=ToolStatus.SUCCESS,
                    message=f"Workflow {workflow_id} completed successfully",
                    output=completion_result['final_stats'],
                    metadata={'workflow_id': workflow_id, 'type': 'workflow_completion'}
                )]

            else:
                response_content = "❌ **Workflow falhou durante execução**\n\n"
                response_content += f"**Objetivo:** {user_input}\n"
                response_content += f"**Status:** {completion_result['status']}\n"
                response_content += f"**Erro:** {completion_result.get('message', 'Erro desconhecido')}\n\n"
                response_content += "Verifique os logs para mais detalhes sobre o erro."

                tool_results = [ToolResult(
                    status=ToolStatus.ERROR,
                    message=f"Workflow {workflow_id} failed",
                    error_message=completion_result.get('message', 'Workflow execution failed'),
                    metadata={'workflow_id': workflow_id, 'type': 'workflow_failure'}
                )]

            return response_content, tool_results

        except Exception as e:
            logger.error(f"Workflow execution failed: {e}")

            error_response = "❌ **Erro na execução do workflow**\n\n"
            error_response += f"**Erro:** {str(e)}\n"
            error_response += "Executando fallback para processamento tradicional..."

            # Fallback para processamento tradicional
            return await self._process_iterative_function_calling(user_input, parse_result, session)

    def reload_skills(self) -> int:
        """Re-scan all skill directories and hot-reload the command registry.

        Called by /skills add|remove so changes take effect in the running session
        without restarting DEILE.  Returns the number of skills registered after reload.
        """
        if self._skill_loader is None:
            return 0
        try:
            count = self._skill_loader.reload_into_registry(self.command_registry)
            self.logger.info("Skills hot-reloaded: %d skill(s) active", count)
            return count
        except Exception as exc:
            self.logger.warning("Skills hot-reload failed: %s", exc)
            return 0

    def _auto_discover_components(self) -> None:
        """Descobre automaticamente tools, parsers e comandos"""
        try:
            self.tool_registry.auto_discover()
            self.parser_registry.auto_discover()

            # Initialize commands
            self.command_registry.auto_discover_builtin_commands()
            self.command_registry.load_commands_from_config()

            # Load user/project skills as slash commands AND start hot-reload.
            # The unified skills subsystem owns both — keeping them wired in
            # one place ensures the watcher and the slash-command bridge see
            # the exact same scan order.
            try:
                from ..commands.settings_manager import SettingsManager
                from ..commands.skill_loader import SkillLoader
                from ..skills.watcher import SkillsWatcher

                project_dir = getattr(self.settings, "working_directory", None)
                _settings_mgr = SettingsManager(
                    project_dir=Path(project_dir) if project_dir else None
                )
                skill_loader = SkillLoader(
                    project_dir=project_dir,
                    settings_manager=_settings_mgr,
                )
                invocable_count = skill_loader.load_into_registry(self.command_registry)
                # Store for hot-reload via /skills add|remove
                self._skill_loader = skill_loader

                # Visible boot summary so the operator sees in the launch log
                # how many skills came from where — bundled (auto-trigger +
                # invoke_skill), user/project (those + slash /<name>). Counts
                # via the unified registry to include bundled, which the
                # loader hides by design (legacy slash-command contract).
                from ..skills.registry import get_skill_registry
                _by_src: dict = {}
                for _sk in get_skill_registry().list_all():
                    _by_src[_sk.source] = _by_src.get(_sk.source, 0) + 1
                self.logger.info(
                    "Skills carregadas: total=%d (%s); %d invocáveis como /<nome>",
                    sum(_by_src.values()),
                    ", ".join(f"{k}={v}" for k, v in sorted(_by_src.items())) or "vazio",
                    invocable_count,
                )

                # The watcher uses the SAME extras the loader saw, so a path
                # added via /skills add is watched too. Failures here are
                # non-fatal — the agent still works without hot-reload.
                try:
                    self._skills_watcher = SkillsWatcher(
                        project_dir=Path(project_dir) if project_dir else None,
                        extra_paths=[
                            p for p in _settings_mgr.get_all_skills_paths() if p.is_dir()
                        ],
                        command_registry=self.command_registry,
                    )
                    self._skills_watcher.start()
                except Exception as _watcher_exc:
                    self.logger.warning("Skills hot-reload not started: %s", _watcher_exc)
            except Exception as _skill_exc:
                self.logger.warning("Skill loading failed: %s", _skill_exc)

            # self.logger.info(
            #     f"Auto-discovery completed: {tools_discovered} tools, "
            #     f"{parsers_discovered} parsers, {builtin_commands + config_commands} commands"
            # )
        except Exception as e:
            self.logger.warning(f"Auto-discovery failed: {e}")
    
    async def _execute_proactive_tools(self, user_input: str, session: AgentSession) -> List[ToolResult]:
        """Wrapper sem streaming — drena o stream e devolve só os ToolResults.

        Usado pelo caminho não-streaming (``process_input``) que não tem para
        onde emitir UnifiedStreamEvents. A lógica real vive em
        ``_execute_proactive_tools_stream``; este método consome o gerador,
        descarta os eventos e retorna a lista final.
        """
        results: List[ToolResult] = []
        async for item in self._execute_proactive_tools_stream(user_input, session):
            if isinstance(item, tuple) and item and item[0] == "results":
                results = item[1]
        return results

    async def _execute_proactive_tools_stream(
        self,
        user_input: str,
        session: AgentSession,
    ) -> AsyncIterator[Any]:
        """Versão streaming de ``_execute_proactive_tools``.

        Yields:
            ``UnifiedStreamEvent`` para cada tool proativa (TOOL_USE_START →
            TOOL_USE_END → TOOL_RESULT) e, ao final, a tupla sentinela
            ``("results", List[ToolResult])`` que o caller deve recolher.

        Por que streamar: tools proativas (read_file, list_files,
        check_file_exists) executam antes do LLM e, sem eventos próprios,
        rodam silenciosas — o usuário só vê uma STAGE genérica. Emitir
        eventos sintéticos torna cada chamada visível no transcript com o
        mesmo formato das tool calls do LLM. Para diferenciar visualmente,
        o tool_name é prefixado com ``proactive:`` e ``proactive_execution``
        vai em ``tool_metadata``.
        """
        # Local import mirrors ``process_input_stream`` — keeps stream_events
        # out of the module-level import graph to avoid the circular import we
        # had before agent → models.stream_events → core.agent.
        from deile.core.models.stream_events import (StreamEventType,
                                                     UnifiedStreamEvent)

        if not self.proactive_analyzer:
            yield ("results", [])
            return

        try:
            self.proactive_analyzer.working_directory = session.working_directory
            intents = self.proactive_analyzer.analyze_input(user_input, session.context_data)

            if not intents:
                yield ("results", [])
                return

            proactive_results: List[ToolResult] = []
            executed_tools: set = set()

            for idx, intent in enumerate(intents):
                if not self.proactive_analyzer.should_execute_proactively(intent):
                    self.logger.debug(
                        f"Skipping proactive intent {intent.action.value} - "
                        f"low confidence ({intent.confidence:.2f})"
                    )
                    continue

                tool_key = (intent.action.value, intent.target)
                if tool_key in executed_tools:
                    continue
                executed_tools.add(tool_key)

                tool_name = self._map_proactive_action_to_tool(intent.action)
                if not tool_name:
                    continue

                if tool_name not in self.tool_registry:
                    self.logger.warning(f"Proactive tool {tool_name} not available")
                    continue

                # Synthetic tool_call_id so the renderer can pair START/END/RESULT.
                tc_id = f"proactive-{idx}-{tool_name}"
                display_name = f"proactive:{tool_name}"

                context = self.proactive_analyzer.create_proactive_context(
                    intent, session.context_data
                )
                # parsed_args is the public part of the ToolContext that we want
                # to surface as the call's "arguments" in the transcript.
                visible_args: Dict[str, Any] = dict(getattr(context, "parsed_args", {}) or {})

                yield UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_START,
                    tool_call_id=tc_id,
                    tool_name=display_name,
                )
                yield UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_END,
                    tool_call_id=tc_id,
                    tool_name=display_name,
                    arguments=visible_args,
                )

                try:
                    result = await self.tool_registry.execute_tool(tool_name, context)
                    result.metadata.update({
                        "proactive_execution": True,
                        "proactive_confidence": intent.confidence,
                        "proactive_reason": intent.context,
                    })
                    proactive_results.append(result)
                    self.logger.debug(
                        f"Proactive execution: {tool_name} with confidence "
                        f"{intent.confidence:.2f}"
                    )
                    status = "success" if result.status == ToolStatus.SUCCESS else "error"
                    summary = (
                        getattr(result, "message", "") or ""
                    )[:200] or ("ok" if status == "success" else "error")
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TOOL_RESULT,
                        tool_call_id=tc_id,
                        tool_name=display_name,
                        tool_status=status,
                        tool_result_summary=summary,
                        tool_result_data=getattr(result, "data", None),
                        tool_metadata={
                            "function_name": tool_name,
                            "tool_call_id": tc_id,
                            "proactive_execution": True,
                        },
                    )
                except Exception as e:
                    self.logger.warning(
                        f"Proactive tool execution failed: {tool_name} - {e}"
                    )
                    error_result = ToolResult(
                        status=ToolStatus.ERROR,
                        error=e,
                        message=f"Proactive execution failed: {str(e)}",
                        metadata={
                            "proactive_execution": True,
                            "proactive_failed": True,
                        },
                    )
                    proactive_results.append(error_result)
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TOOL_RESULT,
                        tool_call_id=tc_id,
                        tool_name=display_name,
                        tool_status="error",
                        tool_result_summary=str(e)[:200],
                        tool_metadata={
                            "function_name": tool_name,
                            "tool_call_id": tc_id,
                            "proactive_execution": True,
                            "proactive_failed": True,
                        },
                    )

            yield ("results", proactive_results)

        except Exception as e:
            self.logger.warning(f"Proactive analysis failed: {e}")
            yield ("results", [])

    def _map_proactive_action_to_tool(self, action: ProactiveAction) -> Optional[str]:
        """Mapeia ação proativa para nome da ferramenta correspondente"""
        mapping = {
            ProactiveAction.READ_FILE: "read_file",
            ProactiveAction.LIST_FILES: "list_files",
            ProactiveAction.LIST_DIRECTORY: "list_files",
            ProactiveAction.CHECK_FILE_EXISTS: "check_file_exists",
            # New autonomous actions
            ProactiveAction.SUGGEST_ALTERNATIVES: "list_files",  # Fallback to listing
            ProactiveAction.CHAIN_LIST_AND_READ: "list_files"   # Start with listing
        }
        return mapping.get(action)

    async def _setup_persona_integration(self) -> None:
        """Set up deep integration between agent and persona systems"""
        if not self.persona_manager:
            return

        try:
            # Integration with context manager
            if hasattr(self.context_manager, 'set_persona_integration'):
                self.context_manager.set_persona_integration(self.persona_manager)
            else:
                # Add persona manager reference to context manager
                self.context_manager.persona_manager = self.persona_manager

            # Integration with tool registry (for persona-specific tool preferences)
            if hasattr(self.tool_registry, 'register_persona_tools'):
                self.tool_registry.register_persona_tools(self.persona_manager)

            # Integration with intent analyzer
            if hasattr(self.intent_analyzer, 'set_persona_context'):
                self.intent_analyzer.set_persona_context(self.persona_manager)

            logger.info("Persona integration setup completed successfully")

        except Exception as e:
            logger.warning(f"Some persona integration features unavailable: {e}")

    def enable_persona_enhancement(self, persona_manager: PersonaManager = None) -> None:
        """Enable persona enhancement for this agent"""
        if persona_manager:
            self.persona_manager = persona_manager
            self.persona_manager.set_memory_manager(getattr(self, 'memory_manager', None))

        self.persona_enhanced = True
        logger.info("Persona enhancement enabled")

    def disable_persona_enhancement(self) -> None:
        """Disable persona enhancement for this agent"""
        self.persona_enhanced = False
        logger.info("Persona enhancement disabled")
    
    def __str__(self) -> str:
        return f"DeileAgent(status={self._status.value}, sessions={len(self._sessions)})"
    
    def __repr__(self) -> str:
        return (f"<DeileAgent: status={self._status.value}, "
                f"requests={self._request_count}, "
                f"sessions={len(self._sessions)}>")


# Função helper para instância singleton
_agent: Optional[DeileAgent] = None


def get_agent() -> DeileAgent:
    """Retorna instância singleton do agente"""
    global _agent
    if _agent is None:
        _agent = DeileAgent()
    return _agent