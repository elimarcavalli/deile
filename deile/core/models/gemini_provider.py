"""Provedor Gemini para o sistema de modelos"""

import logging
from typing import List, Optional, AsyncIterator, Dict, Any
import os
import asyncio
import time
from google import genai
from google.genai.types import (
    FunctionDeclaration,
    GenerateContentConfig,
    Tool,
    HttpOptions,
    AutomaticFunctionCallingConfig
)
from google.genai import types
from google.genai import errors as genai_errors

from .base import ModelProvider, ModelType, ModelSize, ModelMessage, ModelResponse, ModelUsage
from ..exceptions import ModelError, ConfigurationError
from ...storage.debug_logger import get_debug_logger, is_debug_enabled

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
    
    def __init__(
        self, 
        gemini_config=None,
        api_key: Optional[str] = None,
        **config
    ):
        # Carrega configuração dinâmica - ConfigManager é fonte única da verdade
        if gemini_config is None:
            try:
                from ...config.manager import get_config_manager
                config_manager = get_config_manager()
                gemini_config = config_manager.get_config().gemini
                # Recarrega configuração para garantir valores mais recentes
                config_manager.reload_config()
                gemini_config = config_manager.get_config().gemini
            except Exception as e:
                # Fallback APENAS em caso de erro crítico
                from ...config.manager import GeminiConfig
                gemini_config = GeminiConfig()
                import logging
                logging.warning(f"Failed to load ConfigManager, using defaults: {e}")
        
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
            from ...tools.registry import get_tool_registry
            from ...tools.base import SecurityLevel
            
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
            from ...tools.registry import get_tool_registry
            from ...tools.base import SecurityLevel
            
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
    
    def reload_config(self) -> None:
        """Recarrega configuração do ConfigManager (hot-reload)"""
        try:
            from ...config.manager import get_config_manager
            config_manager = get_config_manager()
            config_manager.reload_config()
            
            # Atualiza configuração local
            self.gemini_config = config_manager.get_config().gemini
            self.generation_config = self.gemini_config.generation_config.copy()
            
            # Reinicializa ferramentas disponíveis com nova configuração
            self._available_tools = self._get_available_tools()
            
            import logging
            logging.info("GeminiProvider configuration reloaded successfully")
            
        except Exception as e:
            import logging
            logging.error(f"Failed to reload GeminiProvider configuration: {e}")
    
    @property
    def provider_name(self) -> str:
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
                processed_messages, system_instruction, config, execution_context
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
            
        except genai_errors.ClientError as e:
            execution_time = time.time() - start_time
            error = ModelError(
                "Gemini API rate limit exceeded",
                model_name=self.model_name,
                error_code="RATE_LIMIT_EXCEEDED"
            )
            
            if is_debug_enabled():
                await self.debug_logger.log_error(e, {
                    "provider": "gemini",
                    "execution_time": execution_time,
                    "error_type": "rate_limit"
                })
            
            raise error from e
            
        except Exception as e:
            execution_time = time.time() - start_time
            error = ModelError(
                f"Gemini API error: {str(e)}",
                model_name=self.model_name,
                error_code="API_ERROR"
            )
            
            if is_debug_enabled():
                await self.debug_logger.log_error(e, {
                    "provider": "gemini", 
                    "execution_time": execution_time,
                    "error_type": "api_error",
                    "original_error": str(e)
                })
            
            raise error from e
    
    async def generate_stream(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> AsyncIterator[str]:
        """Gera resposta em streaming (Gemini não suporta nativamente)"""
        # Gemini não tem streaming nativo, então simula
        response = await self.generate(messages, system_instruction, **kwargs)
        
        # Simula streaming dividindo a resposta
        words = response.content.split()
        for i in range(0, len(words), 5):  # 5 palavras por chunk
            chunk = " ".join(words[i:i+5])
            if i + 5 < len(words):
                chunk += " "
            yield chunk
            await asyncio.sleep(0.05)  # Simula delay de streaming
    
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
            response = await self.generate(test_messages)
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
    MAX_TOOL_ITERATIONS = 10

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
                max_output_tokens=self.generation_config.get('max_output_tokens', 8192)
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

        Returns:
            Tupla ``(tool_result, function_response_payload)`` onde:
            - ``tool_result`` é um :class:`ToolResult` (ou ``None`` se a tool
              não foi encontrada / execução falhou na borda) — usado pelo
              orquestrador para display/auditoria.
            - ``function_response_payload`` é um dict serializável JSON pronto
              para ser embrulhado em ``types.Part.from_function_response``.
              Sempre presente; em caso de erro, contém ``{"error": "..."}``
              numa forma que o modelo consegue ler e se recuperar.
        """
        from ...tools.registry import get_tool_registry
        from ...tools.base import ToolContext, ToolResult, ToolStatus

        registry = get_tool_registry()
        tool = registry.get(function_name) if hasattr(registry, "get") else None

        # Tool inexistente (ex.: nome alucinado pelo modelo). Devolvemos um
        # erro estruturado em vez de levantar — o modelo aprende e tenta de
        # novo com nome correto na próxima iteração.
        if tool is None:
            available = sorted(registry._tools.keys()) if hasattr(registry, "_tools") else []
            logger.warning(
                "Function call '%s' not found in registry. Available tools: %s",
                function_name,
                available,
            )
            error_payload = {
                "error": (
                    f"Function '{function_name}' is not registered in this agent. "
                    f"Available tools: {', '.join(available) if available else '(none)'}."
                ),
                "status": "error",
                "error_code": "FUNCTION_NOT_FOUND",
            }
            tool_result = ToolResult(
                status=ToolStatus.ERROR,
                message=error_payload["error"],
                metadata={
                    "function_name": function_name,
                    "arguments": arguments,
                    "error_code": "FUNCTION_NOT_FOUND",
                },
            )
            return tool_result, error_payload

        context = ToolContext(
            user_input="",
            parsed_args=dict(arguments or {}),
            session_data=dict(session_data or {}),
            working_directory=working_directory or ".",
            file_list=[],
            metadata={
                "execution_method": "function_call",
                "function_name": function_name,
                "tool_name": tool.name,
            },
        )

        logger.info(
            "Executing function call '%s' with args=%s (cwd=%s)",
            function_name,
            list(context.parsed_args.keys()),
            context.working_directory,
        )

        try:
            tool_result = await tool.execute(context)
        except Exception as exc:  # pylint: disable=broad-except
            # Falha não-tratada na tool: capturamos para que o modelo veja o
            # erro como function_response em vez de quebrar o turno inteiro.
            logger.error(
                "Tool '%s' raised an unhandled exception: %s",
                function_name,
                exc,
                exc_info=True,
            )
            tool_result = ToolResult(
                status=ToolStatus.ERROR,
                message=f"Unhandled exception in {function_name}: {exc}",
                error=exc,
                metadata={"function_name": function_name},
            )
            return tool_result, {
                "error": str(exc),
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

    async def chat_with_tools(
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
                tool_result, payload = await self.execute_function_call(
                    function_name=call["name"],
                    arguments=call["args"],
                    working_directory=working_directory,
                    session_data=session_data,
                )
                tool_results.append(tool_result)
                function_response_parts.append(
                    types.Part.from_function_response(name=call["name"], response=payload)
                )

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

    def estimate_cost(self, usage: ModelUsage) -> float:
        """Estima custo baseado no uso (valores aproximados)"""
        # Custos aproximados por 1K tokens (podem variar)
        cost_per_1k_input = 0.00125  # $0.00125 por 1K input tokens
        cost_per_1k_output = 0.00375  # $0.00375 por 1K output tokens
        
        input_cost = (usage.prompt_tokens / 1000) * cost_per_1k_input
        output_cost = (usage.completion_tokens / 1000) * cost_per_1k_output
        
        return input_cost + output_cost
    
    def reload_config(self, gemini_config=None) -> None:
        """Recarrega configurações do provider"""
        if gemini_config is None:
            try:
                from ...config import get_config_manager
                gemini_config = get_config_manager().get_config().gemini
            except ImportError:
                return
        
        self.gemini_config = gemini_config
        self.model_name = gemini_config.model_name
        self._initialize_model()
        
        if is_debug_enabled():
            asyncio.create_task(self.debug_logger.log_debug_info(
                category="config_reload",
                data={
                    "provider": "gemini",
                    "new_model": gemini_config.model_name,
                    "generation_config": gemini_config.generation_config
                }
            ))
    
    def get_current_config(self) -> Dict[str, Any]:
        """Retorna configuração atual"""
        return {
            "model_name": self.gemini_config.model_name,
            "generation_config": self.gemini_config.generation_config,
            "tool_config": self.gemini_config.tool_config,
            "safety_settings": self.gemini_config.safety_settings
        }
    
    # Métodos auxiliares privados para novo Google GenAI SDK
    
    async def _generate_with_new_sdk(
        self,
        messages: List[Dict[str, Any]],
        system_instruction: Optional[str],
        config: GenerateContentConfig,
        execution_context: Optional[Dict[str, Any]] = None
    ) -> ModelResponse:
        """Gera conteúdo usando novo Google GenAI SDK"""
        try:
            # Prepara contents para o novo SDK
            contents = self._prepare_contents_for_new_sdk(messages)
            
            # CORREÇÃO: Cria uma instância do modelo com a system_instruction
            # model = genai.GenerativeModel(
            #     model_name=self.gemini_config.model_name,
            #     system_instruction=system_instruction,
            #     generation_config=config  # Passa a configuração aqui
            # )

            # Gera o conteúdo a partir da instância do modelo
            # response = await asyncio.to_thread(
            #     model.generate_content,
            #     contents=contents
            # )

            # # CORREÇÃO: Cria uma instância do modelo sem a system_instruction
            # model = genai.GenerativeModel(
            #     model_name=self.gemini_config.model_name,
            #     generation_config=config  # Passa a configuração aqui
            # )
            
            # response = await asyncio.to_thread(
            #         lambda: model.generate_content(contents=contents)
            #     )

            client = self.client

            response = await client.aio.models.generate_content(
                    model=self.gemini_config.model_name,
                    contents=contents,
                    config=config  # types.GenerateContentConfig(...) ou dict compatível
                )
            
            # Processa Function Calls se presentes
            if hasattr(response, 'candidates') and response.candidates and len(response.candidates) > 0:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                    for part in candidate.content.parts:
                        if hasattr(part, 'function_call'):
                            # Execute function call
                            await self._execute_function_call_new_sdk(
                                part.function_call, execution_context
                            )
            
            # Extrai informações de uso
            usage_metadata = getattr(response, 'usage_metadata', None)
            usage = ModelUsage(
                prompt_tokens=usage_metadata.prompt_token_count if usage_metadata else 0,
                completion_tokens=usage_metadata.candidates_token_count if usage_metadata else 0,
                total_tokens=usage_metadata.total_token_count if usage_metadata else 0
            )
            
            # Extrai conteúdo da resposta de forma robusta
            content = ""
            if hasattr(response, 'text') and response.text:
                content = response.text
            elif hasattr(response, 'candidates') and response.candidates and len(response.candidates) > 0:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                    # Extrai texto das parts
                    text_parts = []
                    for part in candidate.content.parts:
                        if hasattr(part, 'text') and part.text:
                            text_parts.append(part.text)
                    content = ''.join(text_parts)
            
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
            
        except Exception as e:
            logger.error(f"Error in new SDK generation: {e}")
            raise ModelError(
                f"Generation failed with new SDK: {str(e)}",
                model_name=self.model_name,
                error_code="NEW_SDK_ERROR"
            ) from e
    
    def _prepare_contents_for_new_sdk(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Prepara contents para o formato do novo Google GenAI SDK"""
        contents = []
        
        for message in messages:
            # Mapeia roles para novo SDK
            role = message.get("role", "user")
            
            # CORREÇÃO: Remove mensagens system - API Gemini não suporta
            if role == "system":
                continue  # Pula mensagens system
            
            if role == "model":
                role = "assistant"
            
            parts = message.get("parts", [])
            if isinstance(parts, str):
                parts = [{"text": parts}]
            
            contents.append({
                "role": role,
                "parts": parts
            })
        
        return contents
    
    async def _execute_function_call_new_sdk(
        self,
        function_call,
        execution_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Executa function call usando ToolRegistry - Novo SDK"""
        try:
            from ...tools.registry import get_tool_registry
            
            tool_registry = get_tool_registry()
            
            # Extrai nome e argumentos da function call
            function_name = getattr(function_call, 'name', '')
            arguments = dict(getattr(function_call, 'args', {}))
            
            # Executa function call
            result = tool_registry.execute_function_call(
                function_name=function_name,
                arguments=arguments,
                execution_context=execution_context
            )
            
            return {
                "name": function_name,
                "content": result.data if result.is_success else f"Error: {result.message}",
                "success": result.is_success,
                "error": result.message if not result.is_success else None
            }
            
        except Exception as e:
            logger.error(f"Error executing function call '{function_call}': {e}")
            return {
                "name": getattr(function_call, 'name', 'unknown'),
                "content": f"Execution error: {str(e)}",
                "success": False,
                "error": str(e)
            }
    
    def _process_messages_for_gemini(self, messages: List[ModelMessage]) -> List[Dict[str, Any]]:
        """Processa mensagens com suporte a multi-modal input"""
        processed_messages = []
        
        for message in messages:
            # Mapeia roles
            if message.role == "assistant":
                role = "model"
            elif message.role == "system":
                # System messages são tratadas na system_instruction
                continue
            else:
                role = "user"
            
            # Processa content (pode ser string ou lista de parts)
            if isinstance(message.content, str):
                # Texto simples
                parts = [{"text": message.content}]
            elif isinstance(message.content, list):
                # Lista de parts (text + file_data)
                parts = message.content
            else:
                # Fallback para string
                parts = [{"text": str(message.content)}]
            
            processed_messages.append({
                "role": role,
                "parts": parts
            })
        
        return processed_messages
    
    # Métodos antigos removidos - usando novo Google GenAI SDK
    # _generate_with_function_calling() substituído por _generate_with_new_sdk()
    # _extract_function_calls() e _execute_function_call() substituídos por novos métodos
    # Function Calling agora é automático via automatic_function_calling=True
    
    # Métodos auxiliares legacy removidos - novo SDK gerencia Function Calling automaticamente