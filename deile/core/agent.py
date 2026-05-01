"""Agent Orchestrator principal do DEILE"""

from typing import Dict, List, Optional, Any, AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
import asyncio
import logging
import re
import time
from pathlib import Path
from datetime import timedelta

from .exceptions import DEILEError, ToolError, ParserError, ModelError

# Module-level import so that `except _BudgetExceeded:` clauses don't NameError
# if the import inside a try block ever raises (extremely rare but possible).
try:
    from deile.storage.usage_repository import BudgetExceeded as _BudgetExceeded
except Exception:  # pragma: no cover — defensive only
    class _BudgetExceeded(Exception):  # type: ignore[no-redef]
        provider_id = None
        limit_type = None
from .context_manager import ContextManager
from .models.router import ModelRouter
from .intent_analyzer import IntentAnalyzer, get_intent_analyzer
from .proactive_analyzer import ProactiveAnalyzer, get_proactive_analyzer, ProactiveAction
from .file_resolver import get_file_resolver
from ..tools.registry import ToolRegistry, get_tool_registry
from ..tools.base import ToolContext, ToolResult, ToolStatus
from ..parsers.registry import ParserRegistry, get_parser_registry
from ..parsers.base import ParseResult, ParseStatus, ParsedCommand
from ..commands.registry import CommandRegistry, get_command_registry
from ..commands.actions import CommandActions
from ..ui.display_manager import DisplayManager
from ..storage.logs import get_logger
from ..config.settings import get_settings
from ..personas.manager import PersonaManager
from ..orchestration.workflow_executor import get_workflow_executor


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


def _is_permanent_provider_error(exc: Exception) -> bool:
    """Return True for errors that retrying a different provider cannot fix.

    Permanent errors (auth, invalid_request/model_not_found) should bubble up to the
    user so they can correct their configuration instead of silently cascading.
    Transient errors (rate_limit, server) may be retried via cascade.
    """
    from deile.core.models.errors import ProviderInvocationError  # noqa: PLC0415
    if not isinstance(exc, ProviderInvocationError):
        return False
    return exc.envelope.error_type in ("auth", "invalid_request")


def _self_record_circuit(provider_id: str, *, success: bool) -> None:
    """Notify TierRouter's CircuitBreaker of a provider call outcome."""
    try:
        from deile.core.models.tier_router import get_tier_router  # noqa: PLC0415
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
        from deile.storage.debug_logger import get_debug_logger  # noqa: PLC0415
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
    
    def update_activity(self) -> None:
        """Atualiza timestamp da última atividade"""
        self.last_activity = time.time()
    
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


class DeileAgent:
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
        self.command_actions = CommandActions(
            agent=self,
            config_manager=config_manager
        )
        
        self.settings = get_settings()
        self.logger = get_logger()

        self._status = AgentStatus.IDLE
        self._sessions: Dict[str, AgentSession] = {}
        self._request_count = 0

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

        # CORREÇÃO: Registra model providers se não há nenhum
        if len(self.model_router.providers) == 0:
            self._register_default_providers()

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
        
        try:
            # Obtém ou cria sessão
            session = self._get_or_create_session(session_id, **kwargs)
            session.update_activity()
            
            # Adiciona entrada ao histórico
            session.add_to_history("user", user_input)
            
            # Intercepta comandos slash ANTES de processar
            if user_input.strip().startswith('/'):
                return await self._process_slash_command(user_input.strip(), session, start_time)
            
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

    async def process_stream(
        self,
        user_input: str,
        session_id: str = "default",
        **kwargs
    ) -> AsyncIterator[str]:
        """Processa entrada com resposta em streaming
        
        Args:
            user_input: Entrada do usuário
            session_id: ID da sessão
            **kwargs: Parâmetros adicionais
            
        Yields:
            str: Chunks da resposta
        """
        self._status = AgentStatus.PROCESSING
        
        try:
            # Obtém ou cria sessão
            session = self._get_or_create_session(session_id, **kwargs)
            session.update_activity()
            
            # Parsing e execução de tools
            parse_result = await self._parse_input(user_input, session)
            tool_results = await self._execute_tools(parse_result, session)
            
            # Streaming da resposta
            self._status = AgentStatus.GENERATING_RESPONSE
            async for chunk in self._generate_response_stream(
                user_input, parse_result, tool_results, session
            ):
                yield chunk
            
        except Exception as e:
            self._status = AgentStatus.ERROR
            if isinstance(e, _BudgetExceeded):
                yield (
                    f"\n[Budget limit reached ({getattr(e, 'limit_type', 'unknown')}): {str(e)}\n"
                    f"Use /model budget to view limits.]\n"
                )
            else:
                yield f"Error: {str(e)}"
        finally:
            self._status = AgentStatus.IDLE
    
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
    
    # Métodos privados
    
    def _get_or_create_session(self, session_id: str, **kwargs) -> AgentSession:
        """Obtém sessão existente ou cria nova"""
        if session_id not in self._sessions:
            return self.create_session(session_id, **kwargs)
        return self._sessions[session_id]
    
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
            
            # Converte resultado do comando para AgentResponse
            response = AgentResponse(
                content=command_result.content or f"Command /{command_name} executed",
                status=AgentStatus.IDLE,
                tool_results=[],
                parse_result=None,
                execution_time=time.time() - start_time,
                metadata={
                    "command_executed": command_name,
                    "command_status": command_result.status.value,
                    "is_slash_command": True
                }
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
        
        for tool_name in parse_result.tool_requests:
            try:
                # Cria contexto para a tool
                context = ToolContext(
                    user_input=session.conversation_history[-1]["content"] if session.conversation_history else "",
                    parsed_args=parse_result.commands[0].arguments if parse_result.commands else {},
                    session_data=session.context_data,
                    working_directory=str(session.working_directory),
                    file_list=parse_result.file_references
                )
                
                # Executa a tool
                result = await self.tool_registry.execute_tool(tool_name, context)
                tool_results.append(result)
                
                # Display tool result using DisplayManager - SOLVES SITUAÇÃO 2 & 3
                self.display_manager.display_tool_result(tool_name, result)
                
                # self.logger.info(f"Tool {tool_name} executed: {result.status.value}")
                
            except Exception as e:
                error_result = ToolResult(
                    status=ToolStatus.ERROR,
                    error=e,
                    message=f"Failed to execute tool {tool_name}: {str(e)}"
                )
                tool_results.append(error_result)
                self.logger.error(f"Tool execution failed: {e}")
        
        return tool_results
    
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

            # Honor /model use <provider:model_id> override stored on the session
            forced = None  # session-level override (hard error if not registered)
            try:
                forced = session.context_data.get("forced_model")
            except AttributeError:
                forced = None

            # Persistent default_model from api_config.yaml (soft preference — falls
            # back to router if the provider is not registered, e.g. missing API key).
            config_default: Optional[str] = None
            if not forced:
                try:
                    from deile.config.manager import get_config_manager
                    config_default = get_config_manager().get_config().default_model or None
                except Exception:
                    pass

            # Seleciona modelo apropriado (tier-aware quando classificado)
            model_provider = None
            _active_forced = forced or config_default
            if _active_forced and isinstance(_active_forced, str) and ":" in _active_forced:
                _fp_id, _fm_id = _active_forced.split(":", 1)
                for p in self.model_router.providers.values():
                    if (
                        getattr(p, "provider_id", None) == _fp_id
                        and getattr(p, "model_name", None) == _fm_id
                    ):
                        model_provider = p
                        break
                if model_provider is None:
                    if forced:
                        # Session-level /model use — hard error, user chose this explicitly
                        available = sorted({
                            getattr(p, "model_name", "?") for p in self.model_router.providers.values()
                            if getattr(p, "provider_id", None) == _fp_id
                        })
                        raise ModelError(
                            f"Forced model '{forced}' is not registered. "
                            f"Available {_fp_id} models: {available or '(none)'}. "
                            f"Use /model use auto to clear the override.",
                            error_code="FORCED_MODEL_NOT_REGISTERED",
                        )
                    # config default not registered — fall through to router silently
                    logger.debug(
                        "default_model '%s' not registered, falling back to router", config_default
                    )
            if model_provider is None:
                model_provider = await self.model_router.select_provider(
                    context=context,
                    session=session,
                    tier=model_tier,
                )

            # Budget enforcement — BudgetExceeded must propagate; other errors fail open
            try:
                from deile.storage.usage_repository import (
                    get_usage_repository,
                    BudgetGuard,
                    BudgetExceeded,
                )
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
            try:
                from deile.storage.debug_logger import get_debug_logger
                await get_debug_logger().log_router_event(
                    "provider_selected",
                    {
                        "provider_id": model_provider.provider_id,
                        "model_id": getattr(model_provider, "model_name", "unknown"),
                        "tier": getattr(model_tier, "value", "unknown"),
                        "session_id": session.session_id,
                    },
                )
            except Exception:
                pass

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
                    message_parts: List[Any] = [user_input]
                    for file_data in context["file_data_parts"]:
                        if "file_data" in file_data:
                            file_uri = file_data["file_data"]["file_uri"]
                            import google.genai.types as genai_types
                            file_obj = genai_types.File(
                                name=file_uri.split('/')[-1],
                                uri=file_uri,
                                mime_type=file_data["file_data"].get("mime_type", "text/plain"),
                            )
                            message_parts.append(file_obj)
                    message_content = message_parts
                    logger.info("Sent message with %d file attachments", len(context["file_data_parts"]))

                try:
                    content, tool_results = await model_provider._gemini_chat_with_tools(
                        chat=chat,
                        message=message_content,
                        working_directory=str(session.working_directory),
                        session_data=session.context_data,
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
                try:
                    from deile.storage.debug_logger import get_debug_logger
                    await get_debug_logger().log_router_event(
                        "provider_call_completed",
                        {
                            "provider_id": model_provider.provider_id,
                            "tool_calls": len(tool_results),
                            "latency_ms": int((time.time() - _t0) * 1000),
                        },
                    )
                except Exception:
                    pass

                logger.info("Chat session completed with %d tool execution(s)", len(tool_results))
                _record_model_used(session, model_provider)
                return content, tool_results

            # ── New providers: Anthropic / OpenAI / DeepSeek via chat_with_tools ─
            elif hasattr(model_provider, 'chat_with_tools'):
                from deile.core.models.base import ModelMessage  # local import to avoid cycle
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
                        from deile.core.models.tier_router import get_tier_router as _gtr_init
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
                        if config_default and _is_permanent_provider_error(_chat_err):
                            raise
                        try:
                            from deile.core.models.tier_router import get_tier_router as _gtr
                            next_provider = _gtr().select(
                                model_tier, skip_provider_ids=tried_providers
                            )
                        except Exception:
                            raise _chat_err  # cascade exhausted

                        model_provider = next_provider
                        logger.warning(
                            "config_default '%s' failed (%s) — cascading to %s (attempt=%d)",
                            config_default,
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
                try:
                    from deile.storage.debug_logger import get_debug_logger
                    await get_debug_logger().log_router_event(
                        "provider_call_completed",
                        {
                            "provider_id": model_provider.provider_id,
                            "tool_calls": len(tool_results_raw),
                            "latency_ms": latency_ms,
                        },
                    )
                except Exception:
                    pass

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
                # Fallback para providers sem suporte a tools
                logger.debug("Using legacy function calling approach")
                return await self._process_legacy_function_calling(user_input, parse_result, session)

        except Exception as e:
            # BudgetExceeded and structured ModelErrors must reach the caller — they carry
            # context the CLI uses to render Rich panels. _BudgetExceeded is module-level (line 18).
            if isinstance(e, _BudgetExceeded):
                # Emit observability event then propagate
                try:
                    from deile.storage.debug_logger import get_debug_logger
                    await get_debug_logger().log_router_event(
                        "budget_exceeded",
                        {
                            "session_id": session.session_id,
                            "provider_id": getattr(e, "provider_id", "unknown"),
                            "limit_type": getattr(e, "limit_type", "unknown"),
                            "message": str(e),
                        },
                    )
                except Exception:
                    pass
                raise
            # FORCED_MODEL_NOT_REGISTERED also propagates so process_input can build a structured response
            if isinstance(e, ModelError) and getattr(e, "error_code", "") == "FORCED_MODEL_NOT_REGISTERED":
                raise
            self.logger.error(f"Chat session function calling failed: {e}", exc_info=True)
            return f"I encountered an error during function calling: {str(e)}", []

    # ------------------------------------------------------------------
    # Validation gate (anti-hallucination + post-write enforcement)
    # ------------------------------------------------------------------

    _PROMISE_PATTERNS = [
        # Portuguese — actions the model commonly promises but skips
        r"\bvou\s+(?:testar|rodar|executar|validar|verificar|instalar|conferir|checar)\b",
        r"\b(?:testar|rodar|executar|validar|verificar|instalar)\s+(?:agora|isso|isto|esse|essa)\b",
        r"\bdeixa\s+eu\s+(?:testar|rodar|executar|validar|verificar)\b",
        r"\bvamos\s+(?:testar|rodar|executar|validar|verificar)\b",
        # English
        r"\b(?:I'?ll|I\s+will|let\s+me)\s+(?:test|run|verify|check|install|validate|execute)\b",
        r"\b(?:testing|running|executing|validating|verifying|installing)\s+(?:it|that|now|this)\b",
    ]

    _VALIDATION_TOOL_NAMES = {
        "bash_execute", "python_execute", "run_tests", "execute_command_enhanced",
    }

    @classmethod
    def _contains_promise_pattern(cls, text: str) -> bool:
        if not text:
            return False
        # cache compiled patterns lazily on the class
        compiled = getattr(cls, "_PROMISE_RE", None)
        if compiled is None:
            compiled = [re.compile(p, re.IGNORECASE) for p in cls._PROMISE_PATTERNS]
            cls._PROMISE_RE = compiled
        return any(rx.search(text) for rx in compiled)

    @classmethod
    def _detect_unvalidated_writes(
        cls, tool_results: List[ToolResult]
    ) -> List[ToolResult]:
        """Return write_file results for executable files that lack a following validation tool call."""
        # All write_file results that the tool flagged as needing validation
        flagged_writes = [
            tr for tr in tool_results
            if tr.metadata.get("post_write_validation_required") is True
        ]
        if not flagged_writes:
            return []
        # Any subsequent execution tool counts as "the model tried to validate"
        validated = any(
            tr.metadata.get("function_name") in cls._VALIDATION_TOOL_NAMES
            for tr in tool_results
        )
        if validated:
            return []
        return flagged_writes

    async def _apply_validation_gate(
        self,
        *,
        user_input: str,
        parse_result: Optional[ParseResult],
        session: AgentSession,
        content: str,
        tool_results: List[ToolResult],
    ) -> tuple[str, List[ToolResult]]:
        """Re-invoke the model once if it wrote executable code without testing
        or promised an action without taking it. Persona-side rules already ask
        for this; the gate is the deterministic enforcement layer.

        Recursion is impossible: the gate marks the session, runs at most one
        retry, and clears the marker. If the retry still violates, the result
        is returned to the user unaltered — surfacing the failure rather than
        masking it.
        """
        # Single-shot per turn — and re-entry from a workflow path also skips
        if session.context_data.get("_validation_gate_active"):
            return content, tool_results

        unvalidated = self._detect_unvalidated_writes(tool_results)
        # Promise gate only fires on SHORT replies — long explanations may use
        # "vamos testar a hipótese" / "let me check" rhetorically without
        # actually intending to invoke a tool. The gate's value is catching
        # the model saying "vou rodar agora!" and stopping cold.
        promise_without_action = (
            not tool_results
            and len(content) <= 500
            and self._contains_promise_pattern(content)
        )

        if not unvalidated and not promise_without_action:
            return content, tool_results

        if unvalidated:
            paths = [tr.metadata.get("file_path", "?") for tr in unvalidated]
            cmds = [
                tr.metadata.get("post_write_validation_command")
                for tr in unvalidated
                if tr.metadata.get("post_write_validation_command")
            ]
            cmd_block = "\n".join(f"  - {c}" for c in cmds) if cmds else "  (none suggested)"
            gate_prompt = (
                "[INTERNAL_VALIDATION_GATE] You wrote the following executable file(s) "
                "but did not validate them in the same turn:\n"
                f"  {', '.join(paths)}\n\n"
                "Per the Definition of Done, you MUST validate now using the tools. "
                "Suggested validation commands (run via bash_execute):\n"
                f"{cmd_block}\n\n"
                "If validation fails (exit code != 0 or stderr non-empty), diagnose "
                "and fix the file with write_file, then re-validate. Use pip_install "
                "for any ModuleNotFoundError. Only after exit 0 do you report the "
                "task complete to the user — and the report MUST include the actual "
                "validation output, not a summary."
            )
        else:
            gate_prompt = (
                "[INTERNAL_VALIDATION_GATE] Your previous response promised an action "
                "(test / run / install / validate) but no tool was invoked in that "
                "turn. Per the anti-hallucination rule in your persona, that is a "
                "policy violation. Either invoke the tool now to fulfill the promise, "
                "or revise the answer to not promise. Do not produce a final answer "
                "until the action is actually taken."
            )

        # Persist the pre-gate assistant turn so the model sees the gap
        session.add_to_history("assistant", content, {"validation_gate_pre": True})
        session.add_to_history("user", gate_prompt, {"validation_gate": True})
        session.context_data["_validation_gate_active"] = True
        try:
            new_content, new_tool_results = await self._process_iterative_function_calling(
                user_input=gate_prompt,
                parse_result=parse_result,
                session=session,
            )
        finally:
            session.context_data.pop("_validation_gate_active", None)

        return new_content, list(tool_results) + list(new_tool_results)


    async def _process_legacy_function_calling(
        self,
        user_input: str,
        parse_result: Optional[ParseResult],
        session: AgentSession
    ) -> tuple[str, List[ToolResult]]:
        """Fallback para providers sem Chat Session support"""
        try:
            # Executa tools se foram identificadas no parsing
            tool_results = await self._execute_tools(parse_result, session)
            
            # Prepara contexto com tool results
            context = await self.context_manager.build_context(
                user_input=user_input,
                parse_result=parse_result,
                tool_results=tool_results,
                session=session
            )
            
            # Seleciona modelo apropriado
            model_provider = await self.model_router.select_provider(
                context=context,
                session=session
            )
            
            # Gera resposta simples
            if isinstance(context, dict):
                messages = context.get("messages", [])
                system_instruction = context.get("system_instruction")
            else:
                messages = [context] if hasattr(context, 'content') else []
                system_instruction = "You are DEILE, a helpful AI assistant."
            
            response = await model_provider.generate(
                messages=messages,
                system_instruction=system_instruction
            )
            
            return response.content, tool_results
            
        except Exception as e:
            self.logger.error(f"Legacy function calling failed: {e}")
            return f"I encountered an error during processing: {str(e)}", []

    async def _should_create_workflow(self, user_input: str, parse_result: Optional[ParseResult]) -> bool:
        """Determina se deve criar workflow automaticamente usando análise de intenção avançada

        Esta versão refatorada usa o sistema IntentAnalyzer que implementa:
        - Análise léxica com word boundaries (evita falsos positivos)
        - Análise sintática com padrões regex otimizados
        - Análise semântica com embeddings
        - Sistema de confiança probabilística
        - Cache e métricas de performance
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
            logger.error(f"Error in intent analysis for workflow detection: {e}")
            logger.warning("Falling back to legacy workflow detection logic")

            # Fallback para lógica legacy simplificada em caso de erro
            return await self._legacy_workflow_detection(user_input, parse_result)

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

    async def _legacy_workflow_detection(self, user_input: str, parse_result: Optional[ParseResult]) -> bool:
        """Lógica legacy simplificada para detecção de workflow (fallback)"""
        try:
            user_input_lower = user_input.lower()

            # Palavras-chave críticas que sempre indicam workflow
            critical_keywords = ['implementar', 'implement', 'criar sistema', 'create system', 'desenvolver']
            has_critical_keyword = any(keyword in user_input_lower for keyword in critical_keywords)

            # Múltiplas tools sempre indicam workflow
            multiple_tools = parse_result and len(parse_result.tool_requests or []) > 1

            # Complexidade básica
            is_complex = len(user_input.split()) > 15

            return has_critical_keyword or multiple_tools or is_complex

        except Exception as e:
            logger.error(f"Error in legacy workflow detection: {e}")
            return False  # Default conservador

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
                response_content = f"✅ **Workflow concluído com sucesso!**\n\n"
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
                response_content = f"❌ **Workflow falhou durante execução**\n\n"
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

            error_response = f"❌ **Erro na execução do workflow**\n\n"
            error_response += f"**Erro:** {str(e)}\n"
            error_response += "Executando fallback para processamento tradicional..."

            # Fallback para processamento tradicional
            return await self._process_iterative_function_calling(user_input, parse_result, session)

    async def _generate_response_with_function_calling_legacy(
        self,
        user_input: str,
        parse_result: Optional[ParseResult],
        tool_results: List[ToolResult],
        session: AgentSession
    ) -> str:
        """Fase 3: Geração de resposta usando o modelo de IA com Function Calling"""
        self._status = AgentStatus.GENERATING_RESPONSE
        
        try:
            # Prepara contexto para o modelo com suporte a file_data
            context = await self.context_manager.build_context(
                user_input=user_input,
                parse_result=parse_result,
                tool_results=tool_results,
                session=session
            )
            
            # Seleciona modelo apropriado
            model_provider = await self.model_router.select_provider(
                context=context,
                session=session
            )
            
            # Prepara execution context para Function Calling
            execution_context = self._create_execution_context(session, context)
            
            # Gera resposta com Function Calling
            if isinstance(context, dict):
                messages = context.get("messages", [])
                system_instruction = context.get("system_instruction")
                file_data_parts = context.get("file_data_parts", [])
            else:
                # Fallback se context não é dict
                messages = [context] if hasattr(context, 'content') else []
                system_instruction = "You are DEILE, a helpful AI assistant."
                file_data_parts = []
            
            # Log função calling info
            logger.debug(f"Function calling enabled with {len(file_data_parts)} file parts")
            
            response = await model_provider.generate(
                messages=messages,
                system_instruction=system_instruction,
                execution_context=execution_context
            )
            
            return response.content
            
        except Exception as e:
            self.logger.error(f"Response generation with Function Calling failed: {e}")
            return f"I encountered an error generating a response: {str(e)}"
    
    def _create_execution_context(self, session: AgentSession, context: Dict[str, Any]) -> Dict[str, Any]:
        """Cria contexto de execução para Function Calling"""
        return {
            "session_id": session.session_id,
            "working_directory": str(session.working_directory),
            "user_id": session.user_id,
            "session_data": session.context_data,
            "context_metadata": context.get("metadata", {}),
            "file_data_available": len(context.get("file_data_parts", [])) > 0
        }

    async def _generate_response_legacy(
        self,
        user_input: str,
        parse_result: Optional[ParseResult],
        tool_results: List[ToolResult],
        session: AgentSession
    ) -> str:
        """LEGACY: Geração de resposta sem Function Calling (compatibilidade)"""
        self._status = AgentStatus.GENERATING_RESPONSE
        
        try:
            # Prepara contexto para o modelo
            context = await self.context_manager.build_context(
                user_input=user_input,
                parse_result=parse_result,
                tool_results=tool_results,
                session=session
            )
            
            # Seleciona modelo apropriado
            model_provider = await self.model_router.select_provider(
                context=context,
                session=session
            )
            
            # Gera resposta
            if isinstance(context, dict):
                messages = context.get("messages", [])
                system_instruction = context.get("system_instruction")
            else:
                # Fallback se context não é dict
                messages = [context] if hasattr(context, 'content') else []
                system_instruction = "You are DEILE, a helpful AI assistant."
            
            response = await model_provider.generate(
                messages=messages,
                system_instruction=system_instruction
            )
            
            return response.content
            
        except Exception as e:
            self.logger.error(f"Response generation failed: {e}")
            return f"I encountered an error generating a response: {str(e)}"
    
    async def _generate_response_stream(
        self,
        user_input: str,
        parse_result: Optional[ParseResult],
        tool_results: List[ToolResult],
        session: AgentSession
    ) -> AsyncIterator[str]:
        """Geração de resposta em streaming — consome UnifiedStreamEvent de qualquer provider."""
        from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent

        try:
            context = await self.context_manager.build_context(
                user_input=user_input,
                parse_result=parse_result,
                tool_results=tool_results,
                session=session
            )

            model_provider = await self.model_router.select_provider(
                context=context,
                session=session
            )

            if isinstance(context, dict):
                messages = context.get("messages", [])
                system_instruction = context.get("system_instruction")
            else:
                messages = []
                system_instruction = "You are DEILE, a helpful AI assistant."

            async for event in model_provider.generate_stream(
                messages=messages,
                system_instruction=system_instruction
            ):
                if not isinstance(event, UnifiedStreamEvent):
                    # Legacy provider yields raw str — pass through
                    if isinstance(event, str):
                        yield event
                    continue

                if event.type == StreamEventType.TEXT_DELTA:
                    if event.text:
                        yield event.text

                elif event.type == StreamEventType.TOOL_USE_START:
                    if event.tool_name:
                        yield f"\n[tool: {event.tool_name}]\n"

                elif event.type == StreamEventType.TOOL_USE_END:
                    yield "\n"

                elif event.type == StreamEventType.USAGE_FINAL:
                    # Consumed silently — usage recording handled elsewhere
                    pass

                elif event.type == StreamEventType.ERROR:
                    env = event.error_envelope
                    msg = str(env) if env else "unknown streaming error"
                    yield f"\n[error: {msg}]\n"

        except Exception as e:
            yield f"Error in streaming response: {str(e)}"
    
    def _auto_discover_components(self) -> None:
        """Descobre automaticamente tools, parsers e comandos"""
        try:
            tools_discovered = self.tool_registry.auto_discover()
            parsers_discovered = self.parser_registry.auto_discover()
            
            # Initialize commands and register actions
            builtin_commands = self.command_registry.auto_discover_builtin_commands()
            config_commands = self.command_registry.load_commands_from_config()
            self._register_command_actions()
            
            # self.logger.info(
            #     f"Auto-discovery completed: {tools_discovered} tools, "
            #     f"{parsers_discovered} parsers, {builtin_commands + config_commands} commands"
            # )
        except Exception as e:
            self.logger.warning(f"Auto-discovery failed: {e}")
    
    def _register_command_actions(self) -> None:
        """Registra todas as actions dos comandos"""
        try:
            # Registra actions principais
            self.command_registry.register_action('show_help', self.command_actions.show_help)
            self.command_registry.register_action('toggle_debug_mode', self.command_actions.toggle_debug_mode)
            self.command_registry.register_action('clear_session', self.command_actions.clear_session)
            self.command_registry.register_action('show_system_status', self.command_actions.show_system_status)
            self.command_registry.register_action('show_config', self.command_actions.show_config)
            self.command_registry.register_action('execute_bash', self.command_actions.execute_bash)
            
        except Exception as e:
            self.logger.warning(f"Failed to register command actions: {e}")

    def _register_default_providers(self) -> None:
        """Registra model providers padrão se nenhum estiver configurado"""
        try:
            # Registra GeminiProvider se API key disponível
            import os
            if os.getenv("GOOGLE_API_KEY"):
                from .models.gemini_provider import GeminiProvider
                gemini_provider = GeminiProvider()
                self.model_router.register_provider(
                    provider=gemini_provider,
                    priority=1,
                    cost_per_token=0.000125  # Custo aproximado
                )
                logger.info("Registered GeminiProvider")

            # Adicione outros providers aqui no futuro
            # if os.getenv("OPENAI_API_KEY"):
            #     from .models.openai_provider import OpenAIProvider
            #     ...

        except Exception as e:
            logger.warning(f"Failed to register default model providers: {e}")

    async def _execute_proactive_tools(self, user_input: str, session: AgentSession) -> List[ToolResult]:
        """Executa ferramentas proativamente baseado na análise da entrada do usuário"""
        if not self.proactive_analyzer:
            return []

        try:
            # Atualiza working directory do analisador proativo
            self.proactive_analyzer.working_directory = session.working_directory

            # Analisa entrada do usuário para detectar intenções proativas
            intents = self.proactive_analyzer.analyze_input(user_input, session.context_data)

            if not intents:
                return []

            proactive_results = []
            executed_tools = set()  # Para evitar execução duplicada

            for intent in intents:
                # Verifica se deve executar proativamente
                if not self.proactive_analyzer.should_execute_proactively(intent):
                    self.logger.debug(f"Skipping proactive intent {intent.action.value} - low confidence ({intent.confidence:.2f})")
                    continue

                # Evita execução duplicada
                tool_key = (intent.action.value, intent.target)
                if tool_key in executed_tools:
                    continue
                executed_tools.add(tool_key)

                # Mapeia ação proativa para nome da ferramenta
                tool_name = self._map_proactive_action_to_tool(intent.action)
                if not tool_name:
                    continue

                # Verifica se a ferramenta está disponível
                if tool_name not in self.tool_registry._tools:
                    self.logger.warning(f"Proactive tool {tool_name} not available")
                    continue

                try:
                    # Cria contexto para execução proativa
                    context = self.proactive_analyzer.create_proactive_context(intent, session.context_data)

                    # Executa a ferramenta
                    result = await self.tool_registry.execute_tool(tool_name, context)

                    # Adiciona metadados sobre execução proativa
                    result.metadata.update({
                        "proactive_execution": True,
                        "proactive_confidence": intent.confidence,
                        "proactive_reason": intent.context
                    })

                    proactive_results.append(result)

                    # Log da execução proativa (só em debug para não ser verboso)
                    self.logger.debug(f"Proactive execution: {tool_name} with confidence {intent.confidence:.2f}")

                except Exception as e:
                    # Falhas proativas não devem quebrar o fluxo principal
                    self.logger.warning(f"Proactive tool execution failed: {tool_name} - {e}")

                    # Adiciona resultado de erro se necessário
                    error_result = ToolResult(
                        status=ToolStatus.ERROR,
                        error=e,
                        message=f"Proactive execution failed: {str(e)}",
                        metadata={
                            "proactive_execution": True,
                            "proactive_failed": True
                        }
                    )
                    proactive_results.append(error_result)

            return proactive_results

        except Exception as e:
            # Falhas no analisador proativo não devem quebrar o sistema
            self.logger.warning(f"Proactive analysis failed: {e}")
            return []

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

    # =============================================
    # AUTONOMOUS FUNCTIONALITY (PHASE 4)
    # =============================================

    async def process_autonomous_request(self, user_input: str, session: 'AgentSession') -> Optional[str]:
        """
        Process autonomous requests with intelligent file resolution

        This is the main entry point for autonomous functionality that enables
        DEILE to handle natural language file references like "read the readme"
        without requiring exact filenames from the user.
        """
        if not self.proactive_analyzer:
            return None

        try:
            # Analyze if this requires autonomous processing
            intents = await self.proactive_analyzer.analyze_enhanced(user_input)

            if not intents:
                return None

            # Filter for autonomous-eligible intents
            autonomous_intents = [intent for intent in intents if intent.autonomous_eligible]

            if not autonomous_intents:
                return None

            logger.info(f"Found {len(autonomous_intents)} autonomous intent(s)")

            # Execute the highest priority autonomous intent
            highest_priority = max(autonomous_intents, key=lambda x: x.priority)

            return await self._execute_autonomous_intent(highest_priority, session)

        except Exception as e:
            logger.error(f"Error in autonomous processing: {e}")
            return None

    async def _execute_autonomous_intent(self, intent: 'ProactiveIntent', session: 'AgentSession') -> Optional[str]:
        """Execute an autonomous intent with intelligent error recovery"""
        try:
            if intent.action == ProactiveAction.READ_FILE and intent.resolved_file:
                return await self._autonomous_read_file(intent, session)

            elif intent.action == ProactiveAction.CHAIN_LIST_AND_READ:
                return await self._autonomous_chain_list_and_read(intent, session)

            elif intent.action == ProactiveAction.SUGGEST_ALTERNATIVES:
                return await self._autonomous_suggest_alternatives(intent, session)

            else:
                # Fallback to regular proactive execution
                tool_name = self._map_proactive_action_to_tool(intent.action)
                if tool_name:
                    return await self._execute_proactive_tool(tool_name, intent.target, session)

        except Exception as e:
            logger.error(f"Error executing autonomous intent {intent.action}: {e}")

            # Try alternative resolution if available
            if intent.chained_actions:
                for fallback_intent in intent.chained_actions:
                    result = await self._execute_autonomous_intent(fallback_intent, session)
                    if result:
                        return result

        return None

    async def _autonomous_read_file(self, intent: 'ProactiveIntent', session: 'AgentSession') -> Optional[str]:
        """Autonomously read a file using resolved file match"""
        if not intent.resolved_file:
            return None

        try:
            # Execute read_file tool with resolved path
            file_path = str(intent.resolved_file.path)
            result = await self._execute_proactive_tool("read_file", file_path, session)

            if result and intent.resolved_file.confidence < 1.0:
                # Add context about the resolution for transparency
                confidence_msg = f"\n\n*Autonomously resolved '{intent.target}' → '{intent.resolved_file.path.name}' (confidence: {intent.resolved_file.confidence:.1%})*"
                result = result + confidence_msg

            return result

        except Exception as e:
            logger.error(f"Error in autonomous read: {e}")
            return None

    async def _autonomous_suggest_alternatives(self, intent: 'ProactiveIntent', session: 'AgentSession') -> Optional[str]:
        """Provide intelligent alternatives when file resolution fails"""
        try:
            # Get file resolver instance
            from .file_resolver import get_file_resolver
            file_resolver = get_file_resolver(Path.cwd())

            # Get alternative suggestions
            suggestions = file_resolver.suggest_alternatives(intent.target, max_suggestions=5)

            if not suggestions:
                return f"❌ No files matching '{intent.target}' found in current directory."

            # Format suggestions nicely
            suggestion_text = f"🔍 Couldn't find exact match for '{intent.target}'. Here are some alternatives:\n\n"

            for i, match in enumerate(suggestions, 1):
                confidence = f"({match.confidence:.1%})" if match.confidence < 1.0 else ""
                suggestion_text += f"{i}. **{match.path.name}** {confidence}\n   └─ {match.reason}\n\n"

            suggestion_text += "💡 *Tip: Try being more specific, or ask me to read one of these files directly.*"

            return suggestion_text

        except Exception as e:
            logger.error(f"Error generating alternatives: {e}")
            return None

    async def _autonomous_chain_list_and_read(self, intent: 'ProactiveIntent', session: 'AgentSession') -> Optional[str]:
        """Chain list files → resolve → read operations autonomously"""
        try:
            # First, list files to help with resolution
            list_result = await self._execute_proactive_tool("list_files", ".", session)

            if not list_result:
                return None

            # Get file resolver and try to find the best match
            from .file_resolver import get_file_resolver
            file_resolver = get_file_resolver(Path.cwd())

            best_match = file_resolver.get_best_match(intent.target, min_confidence=0.7)

            if best_match:
                # Found a good match, read it
                read_result = await self._execute_proactive_tool("read_file", str(best_match.path), session)

                if read_result:
                    # Combine list + read results with resolution context
                    resolution_context = f"🎯 *Found and read '{best_match.path.name}' (confidence: {best_match.confidence:.1%})*\n\n"
                    return list_result + "\n\n" + resolution_context + read_result

            else:
                # No good match, provide alternatives
                alternatives = await self._autonomous_suggest_alternatives(intent, session)
                return list_result + "\n\n" + (alternatives or "❌ No matching files found.")

        except Exception as e:
            logger.error(f"Error in chain operation: {e}")
            return None

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