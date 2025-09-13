"""Agent Orchestrator principal do DEILE"""

from typing import Dict, List, Optional, Any, AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
import asyncio
import logging
import time
from pathlib import Path

from .exceptions import DEILEError, ToolError, ParserError, ModelError
from .context_manager import ContextManager
from .models.router import ModelRouter
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


logger = logging.getLogger(__name__)


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
    
    def add_to_history(self, role: str, content: str, metadata: Optional[Dict] = None) -> None:
        """Adiciona entrada ao histórico da conversa"""
        entry = {
            "role": role,
            "content": content,
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

        # Auto-discover tools, parsers, and commands
        self._auto_discover_components()

        # CORREÇÃO: Registra model providers se não há nenhum
        if len(self.model_router.providers) == 0:
            self._register_default_providers()

    async def initialize(self) -> None:
        """Inicializa componentes assíncronos do agente"""
        try:
            # Inicializa PersonaManager
            self.persona_manager = PersonaManager()
            await self.persona_manager.initialize()

            # Ativa persona padrão
            await self.persona_manager.switch_persona("developer")

            # Reconecta o context_manager com o PersonaManager
            if hasattr(self.context_manager, 'persona_manager'):
                self.context_manager.persona_manager = self.persona_manager

            logger.info("Agent initialized successfully with PersonaManager")

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
            
            # Fase 1: Parsing da entrada
            parse_result = await self._parse_input(user_input, session)
            
            # Fase 2: Execução iterativa de tools e Function Calling
            response_content, tool_results = await self._process_iterative_function_calling(
                user_input, parse_result, session
            )
            
            # Cria resposta
            response = AgentResponse(
                content=response_content,
                status=AgentStatus.IDLE,
                tool_results=tool_results,
                parse_result=parse_result,
                execution_time=time.time() - start_time
            )
            
            # Adiciona resposta ao histórico
            session.add_to_history("assistant", response_content, {
                "tool_results": len(tool_results),
                "parse_status": parse_result.status.value if parse_result else None,
                "function_calling_enabled": True
            })
            
            self._status = AgentStatus.IDLE
            return response
            
        except Exception as e:
            self._status = AgentStatus.ERROR
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
            "model_router": await self.model_router.get_stats() if hasattr(self.model_router, 'get_stats') else {}
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
        """Processa function calling usando Chat Sessions - versão simplificada"""
        self._status = AgentStatus.GENERATING_RESPONSE
        
        try:
            # Prepara contexto inicial
            context = await self.context_manager.build_context(
                user_input=user_input,
                parse_result=parse_result,
                tool_results=[],
                session=session
            )
            
            # Seleciona modelo apropriado
            model_provider = await self.model_router.select_provider(
                context=context,
                session=session
            )
            
            # Verifica se é GeminiProvider com suporte a Chat Sessions
            if hasattr(model_provider, 'create_chat_session'):
                logger.debug("Using Chat Session for automatic function calling")
                
                # Obtém system instruction do contexto
                system_instruction = None
                if isinstance(context, dict):
                    system_instruction = context.get("system_instruction")
                
                # Cria ou obtém chat session
                chat = await model_provider.create_chat_session(
                    session_id=session.session_id,
                    system_instruction=system_instruction
                )
                
                # Prepara mensagem - inclui file_data se disponível no contexto
                message_content = user_input

                # CORREÇÃO CRÍTICA: Verifica se há file_data_parts no contexto
                if isinstance(context, dict) and "file_data_parts" in context:
                    # Cria mensagem com texto + File objects
                    message_parts = [user_input]  # Primeiro adiciona o texto

                    # Adiciona cada arquivo como File object
                    for file_data in context["file_data_parts"]:
                        if "file_data" in file_data:
                            file_uri = file_data["file_data"]["file_uri"]
                            # Cria File object a partir do URI
                            import google.genai.types as genai_types
                            file_obj = genai_types.File(
                                name=file_uri.split('/')[-1],  # Extrai nome do URI
                                uri=file_uri,
                                mime_type=file_data["file_data"].get("mime_type", "text/plain")
                            )
                            message_parts.append(file_obj)

                    # Envia como lista com string + File objects
                    response = chat.send_message(message_parts)
                    logger.info(f"Sent message with {len(context['file_data_parts'])} file attachments")
                else:
                    # Envia mensagem simples sem anexos
                    response = chat.send_message(user_input)
                
                # Chat Sessions gerenciam tools automaticamente, mas precisamos extrair tool results
                # para compatibilidade com o resto do sistema
                tool_results = await self._extract_tool_results_from_chat_response(response)
                
                # Extrai conteúdo da resposta
                content = ""
                if hasattr(response, 'text'):
                    content = response.text
                elif hasattr(response, 'content'):
                    content = response.content
                else:
                    content = str(response)
                
                logger.info(f"Chat session completed with {len(tool_results)} tool executions")
                return content, tool_results
            
            else:
                # Fallback para providers sem Chat Session support
                logger.debug("Using legacy function calling approach")
                return await self._process_legacy_function_calling(user_input, parse_result, session)
                
        except Exception as e:
            self.logger.error(f"Chat session function calling failed: {e}")
            return f"I encountered an error during function calling: {str(e)}", []


    async def _extract_tool_results_from_chat_response(self, response) -> List[ToolResult]:
        """Extrai tool results de uma resposta de Chat Session"""
        from ..tools.base import ToolResult, ToolStatus
        tool_results = []

        try:
            logger.debug("Extracting tool results from chat session response")

            # Analisa candidates para encontrar function calls
            if hasattr(response, 'candidates') and response.candidates:
                logger.debug(f"Found {len(response.candidates)} candidates in response")

                for i, candidate in enumerate(response.candidates):
                    if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                        logger.debug(f"Candidate {i} has {len(candidate.content.parts)} parts")

                        for j, part in enumerate(candidate.content.parts):
                            if hasattr(part, 'function_call') and part.function_call:
                                # Encontrou function call - criar ToolResult
                                function_call = part.function_call

                                # Proteção contra function_call None ou sem name
                                if function_call and hasattr(function_call, 'name') and function_call.name:
                                    logger.info(f"Found function call: {function_call.name}")

                                    tool_result = ToolResult(
                                        status=ToolStatus.SUCCESS,
                                        message=f"Executed {function_call.name}",
                                        data=dict(function_call.args) if hasattr(function_call, 'args') else {},
                                        metadata={
                                            "function_name": function_call.name,
                                            "candidate_index": i,
                                            "part_index": j
                                        }
                                    )
                                    tool_results.append(tool_result)
                                else:
                                    logger.debug(f"Found function_call without name at candidate {i}, part {j}")

                            elif hasattr(part, 'text'):
                                logger.debug(f"Found text part with {len(part.text)} chars")

            # CORREÇÃO CRÍTICA: Acessa automatic_function_calling_history
            if not tool_results and hasattr(response, 'automatic_function_calling_history'):
                logger.debug(f"Found automatic_function_calling_history with {len(response.automatic_function_calling_history)} entries")

                for entry in response.automatic_function_calling_history:
                    if hasattr(entry, 'parts'):
                        for part in entry.parts:
                            if hasattr(part, 'function_call') and part.function_call:
                                function_call = part.function_call

                                # Proteção contra function_call None ou sem name
                                if function_call and hasattr(function_call, 'name') and function_call.name:
                                    logger.info(f"Found function call in history: {function_call.name}")

                                    tool_result = ToolResult(
                                        status=ToolStatus.SUCCESS,
                                        message=f"Executed {function_call.name}",
                                        data=dict(function_call.args) if hasattr(function_call, 'args') else {},
                                        metadata={"function_name": function_call.name, "from": "automatic_history"}
                                    )
                                    tool_results.append(tool_result)

            # Fallback: tenta examinar outras propriedades do response
            if not tool_results:
                # Verifica se há informações de function calling em outras propriedades
                if hasattr(response, 'function_calls') and response.function_calls:
                    logger.debug(f"Found function_calls property with {len(response.function_calls)} calls")
                    for fc in response.function_calls:
                        tool_result = ToolResult(
                            status=ToolStatus.SUCCESS,
                            message=f"Executed {fc.name}",
                            data=dict(fc.args) if hasattr(fc, 'args') else {},
                            metadata={"function_name": fc.name}
                        )
                        tool_results.append(tool_result)

                # Se ainda não encontrou, verifica response metadata
                elif hasattr(response, 'usage') and response.usage:
                    logger.debug("No explicit function calls found, checking usage metadata")
                    # Se há usage mas sem function calls explícitos, pode indicar que tools foram executadas
                    # mas não capturadas na estrutura padrão

            logger.info(f"Extracted {len(tool_results)} tool results from chat response")

            # Debug: mostra estrutura do response se não encontrou function calls
            if not tool_results:
                logger.debug(f"Response type: {type(response)}")
                logger.debug(f"Response attributes: {dir(response)}")
                if hasattr(response, 'candidates'):
                    logger.debug(f"Candidates type: {type(response.candidates)}")

            return tool_results

        except Exception as e:
            logger.error(f"Error extracting tool results from chat response: {e}")
            logger.debug(f"Response object: {response}")
            return []

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
        """Geração de resposta em streaming"""
        try:
            # Similar ao método anterior, mas com streaming
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
            
            # Corrigido: trata context como dict
            if isinstance(context, dict):
                messages = context.get("messages", [])
                system_instruction = context.get("system_instruction")
            else:
                messages = []
                system_instruction = "You are DEILE, a helpful AI assistant."
            
            async for chunk in model_provider.generate_stream(
                messages=messages,
                system_instruction=system_instruction
            ):
                yield chunk
                
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