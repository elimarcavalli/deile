"""Interface base para provedores de modelos de IA"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, AsyncIterator
from enum import Enum
import time


class ModelType(Enum):
    """Tipos de modelos disponíveis"""
    CHAT = "chat"
    COMPLETION = "completion"
    EMBEDDING = "embedding"
    VISION = "vision"
    CODE = "code"


class ModelSize(Enum):
    """Tamanhos de modelo para routing inteligente"""
    SMALL = "small"    # Para tarefas rápidas e simples
    MEDIUM = "medium"  # Para tarefas balanceadas
    LARGE = "large"    # Para tarefas complexas e críticas


@dataclass
class ModelUsage:
    """Informações de uso do modelo"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_time: float = 0.0
    cost_estimate: float = 0.0


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
        """Nome do provedor (ex: 'gemini', 'openai')"""
        pass
    
    @property
    @abstractmethod
    def supported_types(self) -> List[ModelType]:
        """Tipos de modelo suportados por este provedor"""
        pass
    
    @property
    @abstractmethod
    def model_size(self) -> ModelSize:
        """Tamanho/categoria do modelo para routing"""
        pass
    
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
    
    @abstractmethod
    async def generate_stream(
        self,
        messages: List[ModelMessage], 
        system_instruction: Optional[str] = None,
        **kwargs
    ) -> AsyncIterator[str]:
        """Gera resposta em streaming
        
        Args:
            messages: Lista de mensagens da conversa
            system_instruction: Instrução do sistema (opcional)
            **kwargs: Parâmetros específicos do modelo
            
        Yields:
            str: Chunks da resposta conforme gerada
        """
        # Fallback para modelos que não suportam streaming
        response = await self.generate(messages, system_instruction, **kwargs)
        yield response.content
    
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
        """Estima custo da requisição
        
        Args:
            usage: Informações de uso
            
        Returns:
            float: Custo estimado em USD
        """
        # Implementação básica - deve ser sobrescrita por cada provedor
        return 0.0
    
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