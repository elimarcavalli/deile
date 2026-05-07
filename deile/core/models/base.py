"""Interface base para provedores de modelos de IA"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import (TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional,
                    Tuple)

from deile.core.models.tier import ModelTier

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from deile.core.models.catalog import ModelPricing
    from deile.core.models.stream_events import UnifiedStreamEvent


class ModelType(Enum):
    """Tipos de modelos disponíveis"""
    CHAT = "chat"
    COMPLETION = "completion"
    EMBEDDING = "embedding"
    VISION = "vision"
    CODE = "code"


class ModelSize(Enum):
    """Tamanhos de modelo para routing inteligente (legado — use ModelTier em código novo)."""
    SMALL = "small"    # Para tarefas rápidas e simples
    MEDIUM = "medium"  # Para tarefas balanceadas
    LARGE = "large"    # Para tarefas complexas e críticas


def tier_to_model_size(tier: ModelTier) -> ModelSize:
    """Backward-compat mapping from new ModelTier to legacy ModelSize."""
    _map = {
        ModelTier.TIER_1: ModelSize.LARGE,
        ModelTier.TIER_2: ModelSize.MEDIUM,
        ModelTier.TIER_3: ModelSize.SMALL,
        ModelTier.TIER_4: ModelSize.SMALL,
    }
    return _map[tier]


@dataclass
class ModelUsage:
    """Informações de uso do modelo"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    request_time: float = 0.0
    cost_estimate: float = 0.0
    # Provider-specific extras (e.g. reasoning_content for DeepSeek reasoning models)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelMessage:
    """Mensagem para o modelo"""
    role: str  # 'user', 'assistant', 'system'
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __str__(self) -> str:
        return f"[{self.role}] {self.content[:100]}..."


@dataclass
class ModelResponse:
    """Resposta do modelo de IA"""
    content: str
    model_name: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw_response: Any = None
    finish_reason: Optional[str] = None
    
    @property
    def is_complete(self) -> bool:
        """Verifica se a resposta está completa"""
        return self.finish_reason != "length"
    
    def __str__(self) -> str:
        return f"ModelResponse(model={self.model_name}, tokens={self.usage.total_tokens})"


class ModelProvider(ABC):
    """Interface base abstrata para provedores de modelos de IA
    
    Permite implementações intercambiáveis de diferentes provedores
    (Gemini, OpenAI, Claude, etc.) seguindo o padrão Strategy.
    """
    
    def __init__(self, model_name: str, **config):
        self.model_name = model_name
        self.config = config
        self._request_count = 0
        self._total_tokens = 0
        self._is_available = True
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Nome do provedor (ex: 'gemini', 'openai') — legado, use provider_id em código novo."""
        pass

    @property
    def provider_id(self) -> str:
        """Canonical provider identifier used by the router ('anthropic', 'openai', etc.).

        Defaults to provider_name for backward compat; override in new providers.
        """
        return self.provider_name

    @property
    @abstractmethod
    def supported_types(self) -> List[ModelType]:
        """Tipos de modelo suportados por este provedor"""
        pass

    @property
    @abstractmethod
    def model_size(self) -> ModelSize:
        """Tamanho/categoria do modelo para routing (legado — use tier em código novo)."""
        pass

    @property
    def tier(self) -> ModelTier:
        """Model tier used by the new router.

        Defaults to a backward-compat mapping from model_size; override in new providers.
        """
        size_to_tier = {
            ModelSize.LARGE: ModelTier.TIER_1,
            ModelSize.MEDIUM: ModelTier.TIER_2,
            ModelSize.SMALL: ModelTier.TIER_3,
        }
        return size_to_tier.get(self.model_size, ModelTier.TIER_2)

    @property
    def pricing(self) -> Optional["ModelPricing"]:
        """Pricing info for this model; None until the provider is catalog-aware."""
        return None
    
    @property
    def is_available(self) -> bool:
        """Verifica se o provedor está disponível"""
        return self._is_available
    
    @property
    def request_count(self) -> int:
        """Número total de requisições feitas"""
        return self._request_count
    
    @property
    def total_tokens(self) -> int:
        """Total de tokens utilizados"""
        return self._total_tokens
    
    @abstractmethod
    async def generate(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> ModelResponse:
        """Gera resposta para as mensagens fornecidas
        
        Args:
            messages: Lista de mensagens da conversa
            system_instruction: Instrução do sistema (opcional)
            **kwargs: Parâmetros específicos do modelo
            
        Returns:
            ModelResponse: Resposta gerada pelo modelo
            
        Raises:
            ModelError: Erro específico do modelo
        """
        pass
    
    async def chat_with_tools(
        self,
        messages: List[ModelMessage],
        tools: List[Any],
        system_instruction: Optional[str] = None,
        **kwargs,
    ) -> Tuple[str, List[Any], ModelUsage]:
        """Run a multi-turn tool-use loop and return (text, tool_results, usage).

        Providers that support function calling should override this.
        The default falls back to a plain generate() call with no tools.
        """
        response = await self.generate(messages, system_instruction, **kwargs)
        return response.content, [], response.usage

    @abstractmethod
    async def generate_stream(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        tools: Optional[List[Any]] = None,
        **kwargs,
    ) -> AsyncIterator["UnifiedStreamEvent"]:
        """Stream response as UnifiedStreamEvent objects.

        Args:
            messages: conversation history.
            system_instruction: optional system prompt.
            tools: optional list of ``ToolSchema``. When provided, the provider
                must enable function-calling for this stream and emit
                ``TOOL_USE_START`` / ``TOOL_USE_END`` events for any tool the
                model decides to call. The provider does NOT execute the tool —
                the agent's ``ToolLoopExecutor`` orchestrates that.
            **kwargs: extra provider-specific knobs.

        Yields:
            UnifiedStreamEvent: typed events (TEXT_DELTA, TOOL_USE_*, USAGE_FINAL, ERROR)
        """
        # Default: wrap generate() into a single TEXT_DELTA + USAGE_FINAL.
        # Subclasses that support streaming/tools override this method.
        from deile.core.models.stream_events import (ModelUsageSnapshot,
                                                     StreamEventType,
                                                     UnifiedStreamEvent)
        response = await self.generate(messages, system_instruction, **kwargs)
        yield UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text=response.content)
        yield UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                cached_tokens=response.usage.cached_tokens,
                cost_usd=response.usage.cost_estimate,
            ),
        )

    # ------------------------------------------------------------------
    # Tool-loop adapters — providers that support function-calling override.
    # ------------------------------------------------------------------

    def format_assistant_tool_use_message(
        self,
        pending_tool_calls: List[Tuple[str, str, Dict[str, Any]]],
        text_so_far: str = "",
        reasoning_content: Optional[str] = None,
    ) -> "ModelMessage":
        """Encode an assistant turn that contains pending tool_use blocks.

        Args:
            pending_tool_calls: list of ``(tool_call_id, tool_name, arguments)``
                tuples produced by the current streaming round.
            text_so_far: any free-text content the assistant emitted alongside
                the tool calls (some providers require it to round-trip).

        Returns:
            ``ModelMessage`` ready to be appended to the conversation history
            and sent back to the same provider in the next streaming round.

        Default implementation raises ``NotImplementedError``; providers that
        wire ``tools=`` into ``generate_stream`` MUST override.
        """
        raise NotImplementedError(
            f"{self.provider_id} does not implement format_assistant_tool_use_message; "
            "tool-loop streaming is not supported for this provider."
        )

    def format_tool_result_message(
        self,
        tool_call_id: str,
        tool_name: str,
        payload: Any,
    ) -> "ModelMessage":
        """Encode a tool-execution result back into a provider-native message.

        Each provider's chat protocol expects tool results in a slightly
        different shape (Anthropic: ``tool_result`` content blocks, OpenAI:
        ``role=tool`` messages, Gemini: ``function_response`` parts). This
        adapter centralizes that quirk so the agent's ``ToolLoopExecutor``
        can stay provider-agnostic.

        Default implementation raises ``NotImplementedError``; providers that
        wire ``tools=`` into ``generate_stream`` MUST override.
        """
        raise NotImplementedError(
            f"{self.provider_id} does not implement format_tool_result_message; "
            "tool-loop streaming is not supported for this provider."
        )

    async def validate_config(self) -> bool:
        """Valida a configuração do provedor
        
        Returns:
            bool: True se a configuração é válida
        """
        return True
    
    async def health_check(self) -> bool:
        """Verifica se o provedor está saudável
        
        Returns:
            bool: True se o provedor está funcionando
        """
        try:
            # Teste básico com uma mensagem simples
            test_messages = [ModelMessage(role="user", content="test")]
            response = await self.generate(test_messages)
            return response.content is not None
        except Exception:
            self._is_available = False
            return False
    
    async def get_available_models(self) -> List[str]:
        """Lista modelos disponíveis neste provedor
        
        Returns:
            List[str]: Lista de nomes de modelos disponíveis
        """
        return [self.model_name]
    
    def estimate_tokens(self, text: str) -> int:
        """Estima número de tokens para um texto
        
        Args:
            text: Texto para estimar
            
        Returns:
            int: Estimativa de tokens
        """
        # Estimativa simples baseada em caracteres (pode ser refinada)
        return len(text) // 4
    
    def estimate_cost(self, usage: ModelUsage) -> float:
        """Estimate request cost in USD using catalog pricing when available."""
        p = self.pricing
        if p is None:
            return 0.0
        input_cost = (usage.prompt_tokens / 1_000_000) * p.input_per_1m_usd
        output_cost = (usage.completion_tokens / 1_000_000) * p.output_per_1m_usd
        cached_cost = 0.0
        if usage.cached_tokens and p.cached_input_per_1m_usd is not None:
            cached_cost = (usage.cached_tokens / 1_000_000) * p.cached_input_per_1m_usd
        return round(input_cost + output_cost + cached_cost, 8)

    async def _record_usage(
        self,
        session_id: str,
        usage: ModelUsage,
        latency_ms: int,
        success: bool,
        error_envelope: Optional[Any] = None,
    ) -> None:
        """Persist usage to UsageRepository (wired up in Phase 11).

        No-op until UsageRepository is available; safe to call from all providers.
        """
        try:
            from deile.storage.usage_repository import \
                get_usage_repository  # noqa: PLC0415
            repo = get_usage_repository()
            await repo.record_from_provider(
                provider_id=self.provider_id,
                model_id=self.model_name,
                tier=self.tier,
                session_id=session_id,
                usage=usage,
                latency_ms=latency_ms,
                success=success,
                error_envelope=error_envelope,
            )
        except Exception as exc:
            # Telemetry must fail open — but at DEBUG level so DB corruption / disk-full
            # / schema drift can be diagnosed when the operator turns on debug logging.
            logger.debug(
                "usage record failed (provider=%s, session=%s): %s",
                getattr(self, "provider_id", "?"), session_id, exc,
            )
    
    def _update_stats(self, usage: ModelUsage) -> None:
        """Atualiza estatísticas internas"""
        self._request_count += 1
        self._total_tokens += usage.total_tokens
    
    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do provedor"""
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "model_size": self.model_size.value,
            "supported_types": [t.value for t in self.supported_types],
            "is_available": self.is_available,
            "request_count": self.request_count,
            "total_tokens": self.total_tokens,
            "config": self.config
        }
    
    def __str__(self) -> str:
        return f"{self.provider_name}:{self.model_name}"
    
    def __repr__(self) -> str:
        return f"<ModelProvider: {self.provider_name}:{self.model_name}>"


class EmbeddingProvider(ModelProvider):
    """Provedor especializado para embeddings"""
    
    @property
    def supported_types(self) -> List[ModelType]:
        return [ModelType.EMBEDDING]
    
    @abstractmethod
    async def embed(
        self, 
        texts: List[str],
        **kwargs
    ) -> List[List[float]]:
        """Gera embeddings para os textos fornecidos
        
        Args:
            texts: Lista de textos para embeddings
            **kwargs: Parâmetros específicos
            
        Returns:
            List[List[float]]: Lista de vetores de embedding
        """
        pass
    
    async def embed_single(self, text: str, **kwargs) -> List[float]:
        """Gera embedding para um único texto
        
        Args:
            text: Texto para embedding
            **kwargs: Parâmetros específicos
            
        Returns:
            List[float]: Vetor de embedding
        """
        embeddings = await self.embed([text], **kwargs)
        return embeddings[0] if embeddings else []
    
    async def generate(
        self,
        messages: List[ModelMessage],
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> ModelResponse:
        """Implementação para compatibilidade - usa o último message"""
        if not messages:
            raise ValueError("No messages provided for embedding")
        
        last_message = messages[-1]
        embedding = await self.embed_single(last_message.content, **kwargs)
        
        return ModelResponse(
            content=str(embedding),  # Serializa o embedding como string
            model_name=self.model_name,
            usage=ModelUsage(
                prompt_tokens=self.estimate_tokens(last_message.content),
                completion_tokens=len(embedding),
                total_tokens=self.estimate_tokens(last_message.content) + len(embedding)
            ),
            metadata={"embedding_dimension": len(embedding)}
        )