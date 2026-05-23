"""Provedor Gemini para o sistema de modelos"""

import asyncio
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.types import (AutomaticFunctionCallingConfig,
                                GenerateContentConfig, HttpOptions, Tool)

from ...storage.debug_logger import get_debug_logger, is_debug_enabled
from ..exceptions import ConfigurationError, ModelError
from ..loop_guard import check_tool_call, make_guard, record_tool_outcome
from .base import (DEFAULT_MAX_TOOL_ITERATIONS, ModelMessage, ModelProvider,
                   ModelResponse, ModelSize, ModelType, ModelUsage)
from .error_mapping import make_gemini_envelope
from .errors import ProviderInvocationError
from .tool_execution import (OUTCOME_EXCEPTION, OUTCOME_NOT_FOUND,
                             resolve_and_execute_tool)

logger = logging.getLogger(__name__)


def _stringify_for_model(value: Any) -> Any:
    """Converte ``ToolResult.data`` em algo JSON-serializável para function_response.

    Mantém dict/list/primitivos como estão (preservando estrutura para o modelo)
    e força ``str()`` em qualquer objeto custom — evita falhas de serialização
    do Protobuf na borda do SDK.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _stringify_for_model(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify_for_model(v) for v in value]
    return str(value)


class GeminiProvider(ModelProvider):
    """Provedor para modelos Google Gemini"""
    
    @staticmethod
    def _resolve_init_args(gemini_config, api_key):
        """Normalize GeminiProvider's overloaded positional init arguments.

        GeminiProvider is constructed two ways: the multi-provider bootstrap
        calls ``cls(ModelHandle, ProviderConfig)`` while legacy callers pass
        ``cls(GeminiConfig, api_key)`` (or nothing). This isolates that
        branching so ``__init__`` stays linear. Returns
        ``(handle, gemini_config, api_key)`` where ``handle`` is the
        ``ModelHandle`` when the bootstrap path was taken (else ``None``) and
        ``gemini_config`` is always a resolved config object.
        """
        from types import SimpleNamespace

        from deile.core.models.catalog import ModelHandle
        from deile.core.models.provider_config import ProviderConfig as _PC

        handle = None
        if isinstance(gemini_config, ModelHandle):
            handle = gemini_config
            provider_cfg = api_key  # second positional arg is ProviderConfig in bootstrap
            api_key = (
                os.getenv(provider_cfg.api_key_env)
                if isinstance(provider_cfg, _PC)
                else None
            )
            gemini_config = SimpleNamespace(
                model_name=handle.model_id,
                generation_config={},
                tool_config=None,
            )

        # Carrega configuração dinâmica - ConfigManager é fonte única da verdade
        if gemini_config is None:
            try:
                from ...config.manager import get_config_manager
                config_manager = get_config_manager()
                # Recarrega para garantir os valores mais recentes
                config_manager.reload_config()
                gemini_config = config_manager.get_config().gemini
            except Exception as e:
                # Fallback APENAS em caso de erro crítico
                from ...config.manager import GeminiConfig
                gemini_config = GeminiConfig()
                logger.warning("Failed to load ConfigManager, using defaults: %s", e)

        return handle, gemini_config, api_key

    def __init__(
        self,
        gemini_config=None,
        api_key: Optional[str] = None,
        **config
    ):
        handle, gemini_config, api_key = self._resolve_init_args(gemini_config, api_key)
        if handle is not None:
            self._handle = handle

        super().__init__(gemini_config.model_name, **config)
        
        # Armazena configuração
        self.gemini_config = gemini_config
        
        # Configura API key
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            raise ConfigurationError(
                "Google API Key not found. Please set GOOGLE_API_KEY environment variable",
                config_key="GOOGLE_API_KEY"
            )
        
        # Configura cliente com novo SDK
        # IMPORTANTE: Function Calling (tools) só funciona na versão v1beta
        self.client = genai.Client(
            api_key=self.api_key,
            http_options=HttpOptions(api_version="v1beta")
        )
        
        # Mapeia tamanhos de modelo
        self._model_size_mapping = {
            "gemini-1.5-pro": ModelSize.LARGE,
            "gemini-1.5-pro-latest": ModelSize.LARGE,
            "gemini-1.5-flash": ModelSize.MEDIUM,
            "gemini-1.0-pro": ModelSize.MEDIUM,
        }
        
        # Debug logger
        self.debug_logger = get_debug_logger()
        
        # Armazena configurações para uso posterior
        self.generation_config = self.gemini_config.generation_config.copy()
        self.tool_config = self.gemini_config.tool_config
        
        # Inicializa ferramentas disponíveis
        self._available_tools = self._get_available_tools()
        
        # Health check timing control
        self._last_request_time = 0.0
        self._last_health_check_time = 0.0
        self._health_check_interval = 300.0  # 5 minutos em segundos
        
        # Chat sessions cache (per session_id)
        self._chat_sessions = {}
    
    def _create_generation_config(self, tools: Optional[List[Tool]] = None, **kwargs) -> GenerateContentConfig:
        """Cria configuração para geração de conteúdo"""
        config_params = {**self.generation_config, **kwargs}
        
        # Remove parâmetros que não são suportados pelo novo SDK
        supported_params = {
            'temperature', 'top_k', 'top_p', 'max_output_tokens', 
            'candidate_count', 'stop_sequences'
        }
        filtered_params = {k: v for k, v in config_params.items() if k in supported_params}
        
        # Obtém function declarations para tools
        function_declarations = self._get_tools_for_generate_content()
        
        # Configura automatic function calling apenas se há tools disponíveis
        # HABILITADO: permite que o Gemini execute funções automaticamente
        afc_config = None
        tools_wrapper = None
        if function_declarations:
            afc_config = AutomaticFunctionCallingConfig(
                disable=False,
                maximum_remote_calls=10
            )
            
            tools_wrapper = [{"function_declarations": function_declarations}]
        
        return GenerateContentConfig(
            tools=tools_wrapper,  # Wrapped no formato correto
            automatic_function_calling=afc_config,
            **filtered_params
        )
    
    def _get_tools_for_generate_content(self) -> Optional[List]:
        """Obtém tools no formato correto para generate_content (não Tool objects, mas FunctionDeclaration)"""
        try:
            from ...tools.base import SecurityLevel
            from ...tools.registry import get_tool_registry
            
            tool_registry = get_tool_registry()
            
            # Obtém function declarations diretamente (não Tool objects)
            function_declarations = tool_registry.get_gemini_functions(
                authorized_only=True,
                security_level=SecurityLevel.MODERATE
            )
            
            if function_declarations:
                logger.info(f"Loaded {len(function_declarations)} function declarations for generate_content")
                return function_declarations  # Retorna FunctionDeclaration objects diretamente
            else:
                logger.warning("No function declarations available for generate_content")
                return None
                
        except Exception as e:
            logger.error(f"Failed to load function declarations for generate_content: {e}")
            return None

    def _get_available_tools(self) -> Optional[List[Tool]]:
        """Obtém tools disponíveis para Function Calling - Novo SDK"""
        try:
            from ...tools.base import SecurityLevel
            from ...tools.registry import get_tool_registry
            
            tool_registry = get_tool_registry()
            
            # Obtém function declarations com nível de segurança moderado
            function_declarations = tool_registry.get_gemini_functions(
                authorized_only=True,
                security_level=SecurityLevel.MODERATE
            )
            
            if function_declarations:
                # Converte para Tool objects do novo SDK
                tools = [Tool(function_declarations=[func_decl]) for func_decl in function_declarations]
                logger.info(f"Loaded {len(tools)} tools for Function Calling (New SDK)")
                return tools
            else:
                logger.warning("No tools available for Function Calling")
                return None
                
        except Exception as e:
            logger.error(f"Failed to load tools for Function Calling: {e}")
            return None
    
    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def provider_id(self) -> str:
        return "gemini"

    @property
    def supported_types(self) -> List[ModelType]:
        return [ModelType.CHAT, ModelType.VISION, ModelType.CODE]

    @property
    def model_size(self) -> ModelSize:
        # Identifica tamanho baseado no nome do modelo
        for model_prefix, size in self._model_size_mapping.items():
            if self.model_name.startswith(model_prefix):
                return size
        return ModelSize.MEDIUM  # Default
    
    async def generate(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        execution_context: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> ModelResponse:
        """Gera resposta usando novo Google GenAI SDK com Function Calling support"""
        start_time = time.time()
        system_instruction = self._compose_system_instruction(system_instruction)

        # Atualiza timestamp da última request
        self._last_request_time = start_time
        
        try:
            # Log request se debug ativo
            if is_debug_enabled():
                await self.debug_logger.log_request(
                    messages,
                    metadata={
                        "provider": "gemini",
                        "model": self.model_name,
                        "system_instruction": system_instruction,
                        "execution_context": execution_context,
                        "kwargs": kwargs
                    },
                    config=self.gemini_config.generation_config
                )
            
            # Processa mensagens com suporte a multi-modal (file_data)
            processed_messages = self._process_messages_for_gemini(messages)
            
            # Cria configuração para geração
            config = self._create_generation_config(**kwargs)
            
            # Gera conteúdo usando novo SDK
            response = await self._generate_with_new_sdk(
                processed_messages, system_instruction, config
            )
            
            # Calcula tempo de execução
            execution_time = time.time() - start_time
            
            # Log response se debug ativo
            if is_debug_enabled():
                await self.debug_logger.log_response(
                    response,
                    execution_time=execution_time,
                    request_id=self.debug_logger.request_count
                )
            
            return response
            
        except genai_errors.APIError as e:
            # Model-invocation failure: emit the same typed contract as the
            # Anthropic/OpenAI providers so ToolLoopExecutor and the agent loop
            # can read ``envelope.error_type`` (e.g. context_length_exceeded).
            execution_time = time.time() - start_time
            envelope = make_gemini_envelope(e, self.provider_id, self.model_name)

            if is_debug_enabled():
                await self.debug_logger.log_error(e, {
                    "provider": "gemini",
                    "execution_time": execution_time,
                    "error_type": envelope.error_type,
                })

            raise ProviderInvocationError(envelope) from e

        except ProviderInvocationError:
            # Already typed (e.g. re-raised by ``_generate_with_new_sdk``) —
            # propagate without re-wrapping.
            raise

        except Exception as e:
            # Non-API failure (serialization, processing). Still surface a typed
            # envelope so the contract is uniform; classifies as ``unknown``,
            # which the agent's provider cascade treats as transient/retryable —
            # the same effect the pre-refactor ``ModelError`` had here, since
            # neither is flagged permanent by ``_is_permanent_provider_error``.
            execution_time = time.time() - start_time
            envelope = make_gemini_envelope(e, self.provider_id, self.model_name)

            if is_debug_enabled():
                await self.debug_logger.log_error(e, {
                    "provider": "gemini",
                    "execution_time": execution_time,
                    "error_type": envelope.error_type,
                    "original_error": str(e),
                })

            raise ProviderInvocationError(envelope) from e
    
    async def generate_stream(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        tools: Optional[List[Any]] = None,
        **kwargs,
    ) -> AsyncIterator[Any]:
        """Stream response as UnifiedStreamEvent.

        Gemini's chat.send_message is awaitable but not natively chunked the
        same way Anthropic/OpenAI streams are; when ``tools`` is provided, we
        emit a "lumpy" stream — function_call blocks surface as TOOL_USE_*
        events on the round-trip boundary, while plain text emits as a single
        TEXT_DELTA after the response completes. This is the documented
        degraded path called out in the streaming-UI design doc.
        """
        from deile.core.models.stream_events import (ModelUsageSnapshot,
                                                     StreamEventType,
                                                     UnifiedStreamEvent)

        system_instruction = self._compose_system_instruction(system_instruction)

        if tools:
            # Tool-aware path: build a one-shot chat session with the supplied
            # tools, send the last user message, and translate the response.
            sys_instr = self._extract_system(messages, system_instruction)
            user_msg = self._messages_to_gemini_user_input(messages)

            _session_key = f"_stream_{uuid.uuid4().hex}"
            chat = await self.create_chat_session(
                session_id=_session_key,
                system_instruction=sys_instr,
            )
            try:
                response = await asyncio.to_thread(chat.send_message, user_msg)
            except Exception as exc:  # pylint: disable=broad-except
                # Emit the typed ProviderErrorEnvelope contract — identical to
                # the Anthropic/OpenAI stream error path — so ToolLoopExecutor
                # can read ``error_envelope.error_type`` (context_length_exceeded
                # in particular) instead of an ad-hoc dict with no attributes.
                yield UnifiedStreamEvent(
                    type=StreamEventType.ERROR,
                    error_envelope=make_gemini_envelope(
                        exc, self.provider_id, self.model_name
                    ),
                )
                self._chat_sessions.pop(_session_key, None)
                return

            text = self._extract_response_text(response)
            if text:
                yield UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text=text)

            calls = self._extract_function_calls(response)
            for idx, call in enumerate(calls):
                tool_call_id = f"gemini-{id(response)}-{idx}"
                yield UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_START,
                    tool_call_id=tool_call_id,
                    tool_name=call["name"],
                )
                yield UnifiedStreamEvent(
                    type=StreamEventType.TOOL_USE_END,
                    tool_call_id=tool_call_id,
                    tool_name=call["name"],
                    arguments=dict(call.get("args") or {}),
                )

            usage_metadata = getattr(response, "usage_metadata", None)
            if usage_metadata is not None:
                snap = ModelUsageSnapshot(
                    input_tokens=getattr(usage_metadata, "prompt_token_count", 0) or 0,
                    output_tokens=getattr(usage_metadata, "candidates_token_count", 0) or 0,
                    cached_tokens=0,
                    cost_usd=0.0,
                    model=f"{self.provider_id}:{self.model_name}",
                )
                yield UnifiedStreamEvent(type=StreamEventType.USAGE_FINAL, usage=snap)
            self._chat_sessions.pop(_session_key, None)
            return

        # No tools — Gemini's generate() is a single awaitable with no native
        # chunking, so emit the completed text as one TEXT_DELTA. The streaming
        # renderer accumulates deltas, so a single delta renders correctly; the
        # old word-by-word slicing only added artificial latency (no real I/O).
        response = await self.generate(messages, system_instruction, **kwargs)

        if response.content:
            yield UnifiedStreamEvent(
                type=StreamEventType.TEXT_DELTA, text=response.content
            )

        yield UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                cached_tokens=response.usage.cached_tokens,
                cost_usd=response.usage.cost_estimate,
                model=f"{self.provider_id}:{self.model_name}",
            ),
        )

    # ------------------------------------------------------------------
    # Tool-loop adapters
    # ------------------------------------------------------------------

    def format_assistant_tool_use_message(
        self,
        pending_tool_calls: List[Any],  # List[Tuple[str, str, Dict[str, Any]]]
        text_so_far: str = "",
        reasoning_content: Optional[str] = None,
    ) -> ModelMessage:
        """Gemini chat history is owned by the SDK's chat object, so we cache
        the pending calls in metadata and the agent loop never re-sends this
        message — the chat's curated_history already has the function_call
        record from the previous send_message round."""
        return ModelMessage(
            role="assistant",
            content=text_so_far,
            metadata={
                "_gemini_pending_tool_calls": list(pending_tool_calls),
                "_gemini_history_owned_by_sdk": True,
            },
        )

    def format_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        payload: Any,
    ) -> ModelMessage:
        """Encode a Gemini function_response that the agent loop will hand back
        to ``chat.send_message`` as a Part. Keeps the payload JSON-serializable."""
        return ModelMessage(
            role="user",
            content="",
            metadata={
                "_gemini_function_response": {
                    "name": tool_name,
                    "response": _stringify_for_model(payload),
                    "tool_call_id": tool_call_id,
                }
            },
        )
    
    async def validate_config(self) -> bool:
        """Valida configuração do provedor"""
        try:
            # Testa configuração com uma requisição simples
            test_response = await self.generate([
                ModelMessage(role="user", content="Hello")
            ])
            return bool(test_response.content)
        except Exception:
            return False
    
    async def health_check(self) -> bool:
        """Verifica saúde do provedor com controle inteligente de timing"""
        current_time = time.time()
        
        # Se nunca houve requests, não precisa fazer health check
        if self._last_request_time == 0.0:
            logger.debug("No requests made yet, skipping health check")
            return self._is_available
        
        # Verifica se precisa fazer health check baseado no intervalo e última request
        time_since_last_request = current_time - self._last_request_time
        time_since_last_check = current_time - self._last_health_check_time
        
        # Só faz health check se:
        # 1. Passou do intervalo configurado OU
        # 2. Nunca fez health check antes OU  
        # 3. A última request foi há muito tempo (mais que o intervalo)
        should_check = (
            time_since_last_check >= self._health_check_interval or
            self._last_health_check_time == 0.0 or
            time_since_last_request >= self._health_check_interval
        )
        
        if not should_check:
            logger.debug(f"Health check skipped - time since last check: {time_since_last_check:.1f}s")
            return self._is_available
        
        logger.debug("Performing health check...")
        
        try:
            # Health check simples
            test_messages = [ModelMessage(role="user", content="test")]
            await self.generate(test_messages)
            self._is_available = True
            self._last_health_check_time = current_time
            logger.debug("Health check passed")
            return True
        except Exception as e:
            self._is_available = False
            self._last_health_check_time = current_time
            logger.warning(f"Health check failed: {e}")
            return False
    
    # Limite de iterações do loop de function calling manual.
    # Why: cap defensivo contra loops infinitos quando o modelo encadeia chamadas
    # sem convergir para uma resposta final.
    MAX_TOOL_ITERATIONS = DEFAULT_MAX_TOOL_ITERATIONS

    async def create_chat_session(self, session_id: str, system_instruction: Optional[str] = None) -> Any:
        """Cria ou retorna chat session existente para session_id.

        Usa function calling MANUAL: o SDK recebe FunctionDeclarations completos
        (com schemas dos parâmetros) e a execução de cada call é feita por
        :meth:`chat_with_tools`. AFC nativa é desativada para que erros de
        execução sejam capturados como function_response e o histórico do chat
        seja preservado mesmo em falhas.
        """
        if session_id in self._chat_sessions:
            return self._chat_sessions[session_id]

        try:
            function_declarations = self._get_tools_for_generate_content() or []

            tools_param = (
                [Tool(function_declarations=function_declarations)]
                if function_declarations
                else None
            )

            # AFC desligada: o controle do loop fica em chat_with_tools.
            afc_config = AutomaticFunctionCallingConfig(disable=True)

            config = types.GenerateContentConfig(
                tools=tools_param,
                automatic_function_calling=afc_config,
                system_instruction=system_instruction,
                temperature=self.generation_config.get('temperature', 0.1),
                max_output_tokens=self.generation_config.get('max_output_tokens', 16384)
            )

            chat = self.client.chats.create(
                model=self.model_name,
                config=config
            )

            self._chat_sessions[session_id] = chat
            logger.info(
                "Created chat session for session_id=%s with %d function declarations (manual function calling)",
                session_id,
                len(function_declarations),
            )

            return chat

        except Exception as e:
            logger.error(f"Error creating chat session for {session_id}: {e}")
            raise ModelError(f"[CHAT_SESSION_ERROR] Failed to create chat session: {str(e)}") from e

    async def execute_function_call(
        self,
        function_name: str,
        arguments: Dict[str, Any],
        working_directory: str = ".",
        session_data: Optional[Dict[str, Any]] = None,
    ) -> tuple[Any, Dict[str, Any]]:
        """Executa uma function call resolvida via ToolRegistry.

        A etapa comum (resolução via ToolRegistry, tool-inexistente,
        execução e wrap de exceção) é compartilhada com os demais providers
        via :func:`resolve_and_execute_tool`; este método é um wrapper fino
        que só monta o ``function_response`` payload específico do Gemini.

        Returns:
            Tupla ``(tool_result, function_response_payload)`` onde:
            - ``tool_result`` é um :class:`ToolResult` — usado pelo
              orquestrador para display/auditoria.
            - ``function_response_payload`` é um dict serializável JSON pronto
              para ser embrulhado em ``types.Part.from_function_response``.
              Sempre presente; em caso de erro, contém ``{"error": "..."}``
              numa forma que o modelo consegue ler e se recuperar.
        """
        from ...tools.base import ToolContext

        tool_result, outcome = await resolve_and_execute_tool(
            name=function_name,
            args=arguments,
            not_found_message_fn=lambda n, avail: (
                f"Function '{n}' is not registered in this agent. "
                f"Available tools: {', '.join(avail) if avail else '(none)'}."
            ),
            context_factory=lambda n, a, tool: ToolContext(
                user_input="",
                parsed_args=dict(a or {}),
                session_data=dict(session_data or {}),
                working_directory=working_directory or ".",
                file_list=[],
                metadata={
                    "execution_method": "function_call",
                    "function_name": n,
                    "tool_name": tool.name,
                },
            ),
            not_found_metadata={
                "function_name": function_name,
                "arguments": arguments,
                "error_code": "FUNCTION_NOT_FOUND",
            },
            exception_message_fn=lambda n, exc: f"Unhandled exception in {n}: {exc}",
            exception_metadata={"function_name": function_name},
            log_calls=True,
        )

        # Tool inexistente (ex.: nome alucinado pelo modelo). Devolvemos um
        # erro estruturado em vez de levantar — o modelo aprende e tenta de
        # novo com nome correto na próxima iteração.
        if outcome == OUTCOME_NOT_FOUND:
            return tool_result, {
                "error": tool_result.message,
                "status": "error",
                "error_code": "FUNCTION_NOT_FOUND",
            }

        # Falha não-tratada na tool: capturamos para que o modelo veja o
        # erro como function_response em vez de quebrar o turno inteiro.
        if outcome == OUTCOME_EXCEPTION:
            return tool_result, {
                "error": str(tool_result.error),
                "status": "error",
                "error_code": "EXECUTION_EXCEPTION",
            }

        # Carimba o nome da função no metadata para observabilidade downstream
        # (display_manager, smoke tests, telemetria) sem sobrescrever metadata
        # que a própria tool já populou.
        if tool_result.metadata is None:
            tool_result.metadata = {}
        tool_result.metadata.setdefault("function_name", function_name)

        return tool_result, self._tool_result_to_function_response(tool_result, function_name)

    @staticmethod
    def _tool_result_to_function_response(tool_result: Any, function_name: str) -> Dict[str, Any]:
        """Converte ``ToolResult`` em payload JSON-serializable para function_response.

        O Gemini exige que o ``response`` da Part seja um dict serializável.
        Mantemos apenas chaves estáveis e seguras (sem objetos custom, sem
        tracebacks) para evitar erros de serialização do Protobuf.
        """
        from ...tools.base import ToolStatus

        if tool_result.status == ToolStatus.SUCCESS:
            data = tool_result.data
            payload: Dict[str, Any] = {
                "status": "success",
                "result": _stringify_for_model(data),
            }
            if tool_result.message:
                payload["message"] = tool_result.message
            return payload

        return {
            "status": "error",
            "error": tool_result.message or f"{function_name} failed",
            "error_code": tool_result.metadata.get("error_code", "EXECUTION_ERROR")
            if tool_result.metadata
            else "EXECUTION_ERROR",
        }

    # TODO(streaming-cleanup): once all callers migrate to ToolLoopExecutor + generate_stream(tools=...), this method can be removed. Currently still used by deile/core/agent.py:_process_iterative_function_calling.
    async def chat_with_tools(
        self,
        messages: List[ModelMessage],
        tools: List[Any],
        system_instruction: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[str, List[Any], "ModelUsage"]:
        """Unified multi-provider interface: run the Gemini tool loop and return (text, results, usage).

        Creates an ephemeral in-process chat session (not cached in _chat_sessions) so
        the unified router can call this without needing to manage session lifecycle.
        """
        import time as _time

        start = _time.time()
        sys_instr = self._extract_system(messages, system_instruction)
        user_msg = self._messages_to_gemini_user_input(messages)

        _session_key = f"_unified_{uuid.uuid4().hex}"
        chat = await self.create_chat_session(
            session_id=_session_key,
            system_instruction=sys_instr,
        )
        text, tool_results = await self._gemini_chat_with_tools(
            chat=chat,
            message=user_msg,
            working_directory=kwargs.get("working_directory", "."),
            session_data=kwargs.get("session_data"),
        )
        # Clean up the ephemeral session entry
        self._chat_sessions.pop(_session_key, None)

        usage = ModelUsage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            request_time=_time.time() - start,
        )
        return text, tool_results, usage

    def _messages_to_gemini_user_input(self, messages: List[ModelMessage]) -> str:
        """Flatten the last user message to a plain string for Gemini chat."""
        user_parts = [m.content for m in messages if m.role == "user"]
        return user_parts[-1] if user_parts else ""

    async def _gemini_chat_with_tools(
        self,
        chat: Any,
        message: Any,
        working_directory: str = ".",
        max_iterations: Optional[int] = None,
        session_data: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, list]:
        """Envia ``message`` ao chat e roda o loop manual de function calling.

        Substitui o uso de AFC do SDK por um loop controlado:

        1. ``chat.send_message(message)`` envia o input do usuário.
        2. Se a resposta contém ``function_call`` parts, cada uma é executada
           via :meth:`execute_function_call` e o resultado é devolvido ao chat
           como ``Part.from_function_response``.
        3. O loop repete até a resposta não ter mais function_calls ou até
           ``max_iterations`` ser atingido.

        O método garante que o histórico do chat (``_curated_history``) reflita
        sempre o turno completo — incluindo a entrada do usuário — mesmo
        quando uma function call falha, porque o erro vira function_response e
        ``send_message`` retorna normalmente.

        Returns:
            Tupla ``(text, tool_results)``: texto final agregado do modelo e
            a lista de :class:`ToolResult` de cada call executada (na ordem).
        """
        from ...tools.base import ToolResult, ToolStatus

        cap = max_iterations if max_iterations is not None else self.MAX_TOOL_ITERATIONS
        tool_results: list = []
        text_chunks: list[str] = []
        guard = make_guard(
            session_id=str((session_data or {}).get("session_id", "")) or None
        )
        loop_aborted = False

        # send_message é síncrono no SDK — usamos to_thread para não bloquear o loop.
        response = await asyncio.to_thread(chat.send_message, message)

        for iteration in range(cap):
            function_calls = self._extract_function_calls(response)
            text = self._extract_response_text(response)
            if text:
                text_chunks.append(text)

            if not function_calls:
                logger.debug(
                    "Manual function calling loop finished after %d iteration(s)", iteration
                )
                break

            function_response_parts = []
            for call in function_calls:
                # Loop guard — same defensive logic as the other providers.
                # See deile.core.loop_guard for the detection rules.
                brk = check_tool_call(guard, call["name"], dict(call.get("args") or {}))
                if brk is not None:
                    tool_results.append(brk.tool_result)
                    function_response_parts.append(
                        types.Part.from_function_response(
                            name=call["name"], response=brk.payload
                        )
                    )
                    text_chunks.append(brk.message)
                    loop_aborted = True
                    continue
                tool_result, payload = await self.execute_function_call(
                    function_name=call["name"],
                    arguments=call["args"],
                    working_directory=working_directory,
                    session_data=session_data,
                )
                tool_results.append(tool_result)
                record_tool_outcome(guard, tool_result)
                function_response_parts.append(
                    types.Part.from_function_response(name=call["name"], response=payload)
                )

            if loop_aborted:
                # We refused at least one call; do not invoke send_message
                # again — that would let the model keep iterating against the
                # same hash. The error result and break message we already
                # appended will surface to the user.
                break
            response = await asyncio.to_thread(chat.send_message, function_response_parts)
        else:
            # Loop esgotou o cap sem terminar — o modelo continua querendo chamar tools.
            logger.warning(
                "Manual function calling loop hit max_iterations=%d without convergence", cap
            )
            tail_text = self._extract_response_text(response)
            if tail_text:
                text_chunks.append(tail_text)
            tool_results.append(
                ToolResult(
                    status=ToolStatus.ERROR,
                    message=(
                        f"Tool calling loop exceeded max_iterations={cap}. "
                        "The model did not produce a final answer."
                    ),
                    metadata={"error_code": "MAX_ITERATIONS_EXCEEDED"},
                )
            )

        final_text = "\n".join(t for t in text_chunks if t).strip()
        return final_text, tool_results

    @staticmethod
    def _extract_function_calls(response: Any) -> list[Dict[str, Any]]:
        """Coleta todos os function_calls da resposta atual em ordem."""
        calls: list[Dict[str, Any]] = []
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    calls.append(
                        {
                            "name": fc.name,
                            "args": dict(getattr(fc, "args", {}) or {}),
                        }
                    )
        return calls

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """Extrai texto agregado da resposta, tolerando ausência do helper ``.text``."""
        # ``response.text`` lança quando a resposta é só function_calls; por isso
        # iteramos manualmente.
        chunks: list[str] = []
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                txt = getattr(part, "text", None)
                if txt:
                    chunks.append(txt)
        return "".join(chunks)

    # Métodos auxiliares privados para novo Google GenAI SDK
    
    async def _generate_with_new_sdk(
        self,
        messages: List[Dict[str, Any]],
        system_instruction: Optional[str],
        config: GenerateContentConfig,
    ) -> ModelResponse:
        """Gera conteúdo usando novo Google GenAI SDK"""
        try:
            # ``messages`` já vem no formato de contents do SDK
            # (ver _process_messages_for_gemini).
            response = await self.client.aio.models.generate_content(
                model=self.gemini_config.model_name,
                contents=messages,
                config=config,  # types.GenerateContentConfig(...) ou dict compatível
            )
            
            # Extrai informações de uso
            usage_metadata = getattr(response, 'usage_metadata', None)
            usage = ModelUsage(
                prompt_tokens=usage_metadata.prompt_token_count if usage_metadata else 0,
                completion_tokens=usage_metadata.candidates_token_count if usage_metadata else 0,
                total_tokens=usage_metadata.total_token_count if usage_metadata else 0
            )
            
            # Extrai conteúdo via helper que itera ``candidates[*].content.parts``
            # e pula parts não-textuais (thought_signature, function_call, etc.).
            # Acessar ``response.text`` direto em modelos com "thinking" ativo
            # (gemini-3.x preview, etc.) emite ``Warning: there are non-text parts
            # in the response: ['thought_signature']`` em cada chamada — usar o
            # helper evita esse ruído e ainda garante o fallback consistente.
            content = self._extract_response_text(response)
            
            # Fallback se ainda não temos conteúdo
            if not content:
                content = "I apologize, but I couldn't generate a proper response. Please try again."
            
            # Cria resposta final
            model_response = ModelResponse(
                content=content,
                model_name=self.model_name,
                usage=usage,
                raw_response=response,
                finish_reason=getattr(response.candidates[0], 'finish_reason', None) if hasattr(response, 'candidates') and response.candidates and len(response.candidates) > 0 else None,
                metadata={
                    "generation_config": config,
                    "sdk_version": "google-genai"
                }
            )
            
            # Atualiza estatísticas
            self._update_stats(usage)
            
            return model_response
            
        except genai_errors.APIError:
            # API-level failure: let it propagate untouched so ``generate``
            # classifies it into a typed ProviderErrorEnvelope.
            raise
        except Exception as e:
            logger.error(f"Error in new SDK generation: {e}")
            raise ModelError(
                f"Generation failed with new SDK: {str(e)}",
                model_name=self.model_name,
                error_code="NEW_SDK_ERROR"
            ) from e
    
    def _process_messages_for_gemini(self, messages: List[ModelMessage]) -> List[Dict[str, Any]]:
        """Converte ``ModelMessage`` em ``contents`` para o Google GenAI SDK.

        Mapeia ``assistant`` para o role ``model`` que o SDK do Google GenAI
        exige (única alternativa válida a ``user`` — a documentação oficial
        lista somente ``user`` e ``model``; mandar ``assistant`` causa
        400 ``Please use a valid role: user, model``). Mensagens ``system``
        são descartadas (tratadas via ``system_instruction``). ``content`` é
        normalizado em ``parts`` (string/objeto único → lista de parts; lista
        multi-modal mantida).
        """
        contents: List[Dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                # System messages são tratadas na system_instruction.
                continue
            role = "model" if message.role == "assistant" else "user"

            # Processa content (pode ser string ou lista de parts)
            if isinstance(message.content, str):
                parts = [{"text": message.content}]
            elif isinstance(message.content, list):
                # Lista de parts (text + file_data)
                parts = message.content
            else:
                parts = [{"text": str(message.content)}]

            contents.append({"role": role, "parts": parts})

        return contents
