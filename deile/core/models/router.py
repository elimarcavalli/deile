"""Model Router para seleção inteligente de modelos"""

from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
from enum import Enum
import logging
import asyncio
import time
from collections import defaultdict

from .base import ModelProvider, ModelType, ModelSize, ModelMessage, ModelResponse
from ..exceptions import ModelError, ConfigurationError


logger = logging.getLogger(__name__)


class RoutingStrategy(Enum):
    """Estratégias de roteamento de modelos"""
    ROUND_ROBIN = "round_robin"           # Alternância simples
    LEAST_BUSY = "least_busy"             # Modelo menos ocupado
    TASK_OPTIMIZED = "task_optimized"     # Otimizado para o tipo de tarefa
    COST_OPTIMIZED = "cost_optimized"     # Otimizado para menor custo
    PERFORMANCE_OPTIMIZED = "performance" # Otimizado para performance
    LOAD_BALANCED = "load_balanced"       # Balanceamento de carga


@dataclass
class RoutingContext:
    """Contexto para decisão de roteamento"""
    user_input: str
    estimated_tokens: int = 0
    task_type: Optional[str] = None
    priority: str = "normal"  # low, normal, high, critical
    session_data: Dict[str, Any] = None
    performance_requirements: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.session_data is None:
            self.session_data = {}
        if self.performance_requirements is None:
            self.performance_requirements = {}


@dataclass
class ModelMetrics:
    """Métricas de um modelo"""
    total_requests: int = 0
    active_requests: int = 0
    avg_response_time: float = 0.0
    error_rate: float = 0.0
    cost_per_token: float = 0.0
    success_rate: float = 1.0
    last_used: float = 0.0
    
    def update_response_time(self, response_time: float) -> None:
        """Atualiza tempo médio de resposta"""
        if self.total_requests == 0:
            self.avg_response_time = response_time
        else:
            self.avg_response_time = (
                (self.avg_response_time * self.total_requests + response_time) /
                (self.total_requests + 1)
            )
    
    def record_request(self) -> None:
        """Registra nova requisição"""
        self.total_requests += 1
        self.active_requests += 1
        self.last_used = time.time()
    
    def record_completion(self, success: bool, response_time: float) -> None:
        """Registra conclusão de requisição"""
        self.active_requests = max(0, self.active_requests - 1)
        self.update_response_time(response_time)
        
        if success:
            self.success_rate = (
                (self.success_rate * (self.total_requests - 1) + 1.0) /
                self.total_requests
            )
        else:
            self.success_rate = (
                (self.success_rate * (self.total_requests - 1) + 0.0) /
                self.total_requests
            )
            
        self.error_rate = 1.0 - self.success_rate


class ModelRouter:
    """Roteador inteligente de modelos de IA
    
    Responsável por:
    - Seleção automática do melhor modelo para cada tarefa
    - Balanceamento de carga entre modelos
    - Otimização de custo e performance
    - Fallback automático em caso de falha
    """
    
    def __init__(self, default_strategy: RoutingStrategy = RoutingStrategy.TASK_OPTIMIZED):
        self.providers: Dict[str, ModelProvider] = {}
        self.metrics: Dict[str, ModelMetrics] = {}
        self.strategy = default_strategy
        
        # Configurações
        self.fallback_enabled = True
        self.health_check_interval = 300  # 5 minutos
        self.circuit_breaker_enabled = True
        self.circuit_breaker_threshold = 0.8  # 80% de erro para abrir circuito
        
        # Estado interno
        self._round_robin_index = 0
        self._circuit_breaker_status: Dict[str, bool] = {}  # True = aberto (bloqueado)
        self._last_health_check = 0
        
        # Mapeamento de tarefas para tipos de modelo
        self.task_model_mapping = {
            "code_analysis": ModelSize.MEDIUM,
            "code_generation": ModelSize.LARGE,
            "file_summary": ModelSize.SMALL,
            "complex_reasoning": ModelSize.LARGE,
            "simple_questions": ModelSize.SMALL,
            "translation": ModelSize.MEDIUM,
            "embedding": ModelType.EMBEDDING
        }
        
        # Funções de decisão customizáveis
        self.custom_routing_functions: List[Callable[[RoutingContext, List[ModelProvider]], Optional[ModelProvider]]] = []
        
        # (f"ModelRouter initialized with strategy: {default_strategy.value}")
    
    def register_provider(
        self, 
        provider: ModelProvider, 
        priority: int = 0,
        cost_per_token: float = 0.0
    ) -> None:
        """Registra um provedor de modelo
        
        Args:
            provider: Instância do provedor
            priority: Prioridade (maior = preferência)
            cost_per_token: Custo por token para otimização
        """
        provider_key = f"{provider.provider_name}:{provider.model_name}"
        
        if provider_key in self.providers:
            logger.warning(f"Provider {provider_key} already registered, replacing")
        
        self.providers[provider_key] = provider
        self.metrics[provider_key] = ModelMetrics(cost_per_token=cost_per_token)
        self._circuit_breaker_status[provider_key] = False
        
        # (f"Registered model provider: {provider_key}")
    
    def unregister_provider(self, provider_key: str) -> bool:
        """Remove um provedor"""
        if provider_key in self.providers:
            del self.providers[provider_key]
            del self.metrics[provider_key]
            del self._circuit_breaker_status[provider_key]
            # (f"Unregistered provider: {provider_key}")
            return True
        return False
    
    async def select_provider(
        self, 
        context: Optional[Dict[str, Any]] = None,
        session: Optional[Any] = None,
        routing_context: Optional[RoutingContext] = None
    ) -> ModelProvider:
        """Seleciona o melhor provedor para o contexto dado
        
        Args:
            context: Contexto da requisição
            session: Sessão do agente
            routing_context: Contexto específico de roteamento
            
        Returns:
            ModelProvider: Provedor selecionado
            
        Raises:
            ModelError: Se nenhum provedor está disponível
        """
        # Verifica saúde dos provedores periodicamente
        await self._health_check_if_needed()
        
        # Obtém provedores disponíveis
        available_providers = await self._get_available_providers()
        
        # Debug logging
        logger.debug(f"Total providers: {len(self.providers)}")
        logger.debug(f"Available providers: {len(available_providers)}")
        for key, provider in self.providers.items():
            logger.debug(f"Provider {key}: circuit_breaker={self._circuit_breaker_status.get(key, False)}")
        
        if not available_providers:
            raise ModelError(
                f"No model providers available. Total: {len(self.providers)}, Available: {len(available_providers)}", 
                error_code="NO_PROVIDERS"
            )
        
        # Cria contexto de roteamento se não fornecido
        if routing_context is None:
            routing_context = self._create_routing_context(context, session)
        
        # Aplica funções de roteamento customizadas primeiro
        for custom_func in self.custom_routing_functions:
            try:
                selected = custom_func(routing_context, available_providers)
                if selected:
                    logger.debug(f"Custom routing function selected: {selected}")
                    return selected
            except Exception as e:
                logger.warning(f"Custom routing function failed: {e}")
        
        # Aplica estratégia de roteamento padrão
        selected_provider = await self._apply_routing_strategy(
            routing_context, available_providers
        )
        
        if not selected_provider:
            # Fallback: primeiro provedor disponível
            selected_provider = available_providers[0]
            logger.warning("Using fallback provider selection")
        
        provider_key = f"{selected_provider.provider_name}:{selected_provider.model_name}"
        self.metrics[provider_key].record_request()
        
        logger.debug(f"Selected provider: {provider_key} (strategy: {self.strategy.value})")
        return selected_provider
    
    async def execute_with_fallback(
        self,
        messages: List[ModelMessage],
        context: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        **kwargs
    ) -> ModelResponse:
        """Executa requisição com fallback automático
        
        Args:
            messages: Mensagens para o modelo
            context: Contexto da requisição
            max_retries: Número máximo de tentativas
            **kwargs: Parâmetros adicionais
            
        Returns:
            ModelResponse: Resposta do modelo
        """
        last_error = None
        tried_providers = set()
        
        for attempt in range(max_retries):
            try:
                # Seleciona provedor (exclui os que já falharam)
                available_providers = await self._get_available_providers()
                available_providers = [
                    p for p in available_providers 
                    if f"{p.provider_name}:{p.model_name}" not in tried_providers
                ]
                
                if not available_providers:
                    break
                
                provider = await self.select_provider(context)
                provider_key = f"{provider.provider_name}:{provider.model_name}"
                
                # Tenta executar
                start_time = time.time()
                response = await provider.generate(messages, **kwargs)
                execution_time = time.time() - start_time
                
                # Registra sucesso
                self.metrics[provider_key].record_completion(True, execution_time)
                
                logger.debug(f"Request successful with {provider_key} (attempt {attempt + 1})")
                return response
                
            except Exception as e:
                provider_key = f"{provider.provider_name}:{provider.model_name}" if 'provider' in locals() else "unknown"
                execution_time = time.time() - start_time if 'start_time' in locals() else 0
                
                # Registra falha
                if provider_key in self.metrics:
                    self.metrics[provider_key].record_completion(False, execution_time)
                
                tried_providers.add(provider_key)
                last_error = e
                
                logger.warning(f"Request failed with {provider_key} (attempt {attempt + 1}): {e}")
                
                # Atualiza circuit breaker
                if self.circuit_breaker_enabled and provider_key in self.metrics:
                    error_rate = self.metrics[provider_key].error_rate
                    if error_rate >= self.circuit_breaker_threshold:
                        self._circuit_breaker_status[provider_key] = True
                        logger.warning(f"Circuit breaker opened for {provider_key} (error rate: {error_rate:.2%})")
        
        # Todas as tentativas falharam
        raise ModelError(
            f"All model providers failed after {max_retries} attempts. Last error: {str(last_error)}",
            error_code="ALL_PROVIDERS_FAILED"
        ) from last_error
    
    def add_custom_routing_function(self, func: Callable[[RoutingContext, List[ModelProvider]], Optional[ModelProvider]]) -> None:
        """Adiciona função de roteamento customizada"""
        self.custom_routing_functions.append(func)
    
    def set_task_model_mapping(self, task: str, model_preference: ModelSize) -> None:
        """Define mapeamento de tarefa para tipo de modelo"""
        self.task_model_mapping[task] = model_preference
    
    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do router"""
        provider_stats = {}
        for key, metrics in self.metrics.items():
            provider_stats[key] = {
                "total_requests": metrics.total_requests,
                "active_requests": metrics.active_requests,
                "avg_response_time": metrics.avg_response_time,
                "error_rate": metrics.error_rate,
                "success_rate": metrics.success_rate,
                "circuit_breaker_open": self._circuit_breaker_status.get(key, False),
                "last_used": metrics.last_used
            }
        
        return {
            "strategy": self.strategy.value,
            "total_providers": len(self.providers),
            "available_providers": len(await self._get_available_providers()),
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "fallback_enabled": self.fallback_enabled,
            "provider_stats": provider_stats
        }
    
    # Métodos privados
    
    async def _get_available_providers(self) -> List[ModelProvider]:
        """Obtém provedores disponíveis (não bloqueados por circuit breaker)"""
        available = []
        for key, provider in self.providers.items():
            # Verifica circuit breaker
            if self._circuit_breaker_status.get(key, False):
                continue
            
            # Sempre considera disponível se está registrado (simplificação inicial)
            available.append(provider)
        
        return available
    
    def _create_routing_context(
        self, 
        context: Optional[Dict[str, Any]], 
        session: Optional[Any]
    ) -> RoutingContext:
        """Cria contexto de roteamento a partir do contexto geral"""
        user_input = ""
        estimated_tokens = 0
        
        if context:
            if isinstance(context, dict):
                messages = context.get("messages", [])
                if messages:
                    last_message = messages[-1]
                    if hasattr(last_message, 'content'):
                        user_input = last_message.content
                    elif isinstance(last_message, dict):
                        user_input = last_message.get("content", "")
                    else:
                        user_input = str(last_message)
                estimated_tokens = context.get("estimated_tokens", len(user_input) // 4)
            else:
                user_input = str(context)
        
        return RoutingContext(
            user_input=user_input,
            estimated_tokens=estimated_tokens,
            session_data=session.context_data if session and hasattr(session, 'context_data') else {}
        )
    
    async def _apply_routing_strategy(
        self, 
        context: RoutingContext, 
        providers: List[ModelProvider]
    ) -> Optional[ModelProvider]:
        """Aplica estratégia de roteamento selecionada"""
        
        if self.strategy == RoutingStrategy.ROUND_ROBIN:
            return self._round_robin_selection(providers)
        
        elif self.strategy == RoutingStrategy.LEAST_BUSY:
            return self._least_busy_selection(providers)
        
        elif self.strategy == RoutingStrategy.TASK_OPTIMIZED:
            return self._task_optimized_selection(context, providers)
        
        elif self.strategy == RoutingStrategy.COST_OPTIMIZED:
            return self._cost_optimized_selection(context, providers)
        
        elif self.strategy == RoutingStrategy.PERFORMANCE_OPTIMIZED:
            return self._performance_optimized_selection(providers)
        
        elif self.strategy == RoutingStrategy.LOAD_BALANCED:
            return self._load_balanced_selection(providers)
        
        else:
            logger.warning(f"Unknown routing strategy: {self.strategy}")
            return providers[0] if providers else None
    
    def _round_robin_selection(self, providers: List[ModelProvider]) -> ModelProvider:
        """Seleção round-robin"""
        if not providers:
            return None
        
        selected = providers[self._round_robin_index % len(providers)]
        self._round_robin_index += 1
        return selected
    
    def _least_busy_selection(self, providers: List[ModelProvider]) -> ModelProvider:
        """Seleciona provedor menos ocupado"""
        if not providers:
            return None
        
        least_busy = min(
            providers,
            key=lambda p: self.metrics[f"{p.provider_name}:{p.model_name}"].active_requests
        )
        return least_busy
    
    def _task_optimized_selection(
        self, 
        context: RoutingContext, 
        providers: List[ModelProvider]
    ) -> ModelProvider:
        """Seleciona provedor otimizado para a tarefa"""
        # Identifica tipo de tarefa baseado no input
        task_type = self._identify_task_type(context.user_input)
        preferred_size = self.task_model_mapping.get(task_type, ModelSize.MEDIUM)
        
        # Filtra provedores pelo tamanho preferido
        preferred_providers = [
            p for p in providers 
            if p.model_size == preferred_size
        ]
        
        if preferred_providers:
            # Seleciona o com melhor taxa de sucesso entre os preferidos
            return max(
                preferred_providers,
                key=lambda p: self.metrics[f"{p.provider_name}:{p.model_name}"].success_rate
            )
        
        # Fallback: melhor provedor geral
        return max(
            providers,
            key=lambda p: self.metrics[f"{p.provider_name}:{p.model_name}"].success_rate
        )
    
    def _cost_optimized_selection(
        self, 
        context: RoutingContext, 
        providers: List[ModelProvider]
    ) -> ModelProvider:
        """Seleciona provedor com melhor custo-benefício"""
        if not providers:
            return None
        
        # Calcula custo estimado para cada provedor
        def calculate_cost_score(provider: ModelProvider) -> float:
            key = f"{provider.provider_name}:{provider.model_name}"
            metrics = self.metrics[key]
            
            # Custo = cost_per_token * estimated_tokens / success_rate
            # (penaliza provedores com alta taxa de erro)
            if metrics.success_rate == 0:
                return float('inf')
            
            cost = (metrics.cost_per_token * context.estimated_tokens) / metrics.success_rate
            return cost
        
        return min(providers, key=calculate_cost_score)
    
    def _performance_optimized_selection(self, providers: List[ModelProvider]) -> ModelProvider:
        """Seleciona provedor com melhor performance"""
        if not providers:
            return None
        
        # Score baseado em tempo de resposta e taxa de sucesso
        def performance_score(provider: ModelProvider) -> float:
            key = f"{provider.provider_name}:{provider.model_name}"
            metrics = self.metrics[key]
            
            if metrics.avg_response_time == 0 or metrics.success_rate == 0:
                return 0
            
            # Score = success_rate / response_time (maior = melhor)
            return metrics.success_rate / metrics.avg_response_time
        
        return max(providers, key=performance_score)
    
    def _load_balanced_selection(self, providers: List[ModelProvider]) -> ModelProvider:
        """Seleção com balanceamento de carga"""
        if not providers:
            return None
        
        # Combina least_busy com performance
        def load_score(provider: ModelProvider) -> float:
            key = f"{provider.provider_name}:{provider.model_name}"
            metrics = self.metrics[key]
            
            # Score inverso: menor = melhor
            load_factor = metrics.active_requests + 1
            performance_factor = metrics.avg_response_time if metrics.avg_response_time > 0 else 1
            
            return load_factor * performance_factor / max(metrics.success_rate, 0.1)
        
        return min(providers, key=load_score)
    
    def _identify_task_type(self, user_input: str) -> str:
        """Identifica tipo de tarefa baseado na entrada do usuário"""
        input_lower = user_input.lower()
        
        # Análise de padrões simples - pode ser refinada
        if any(word in input_lower for word in ["analyze", "review", "check", "examine"]):
            return "code_analysis"
        elif any(word in input_lower for word in ["create", "generate", "write", "implement"]):
            return "code_generation"
        elif any(word in input_lower for word in ["summarize", "explain", "what is"]):
            return "file_summary"
        elif len(user_input) > 500 or any(word in input_lower for word in ["complex", "detailed", "comprehensive"]):
            return "complex_reasoning"
        else:
            return "simple_questions"
    
    async def _health_check_if_needed(self) -> None:
        """Executa health check se necessário"""
        current_time = time.time()
        if current_time - self._last_health_check < self.health_check_interval:
            return
        
        self._last_health_check = current_time
        
        # Health check de todos os provedores
        for key, provider in self.providers.items():
            try:
                is_healthy = await provider.health_check()
                if is_healthy:
                    # Reseta circuit breaker se estava aberto
                    if self._circuit_breaker_status.get(key, False):
                        self._circuit_breaker_status[key] = False
                        # (f"Circuit breaker reset for {key}")
                else:
                    logger.warning(f"Health check failed for {key}")
            except Exception as e:
                logger.error(f"Health check error for {key}: {e}")


# Singleton instance
_model_router: Optional[ModelRouter] = None


def get_model_router() -> ModelRouter:
    """Retorna instância singleton do ModelRouter"""
    global _model_router
    if _model_router is None:
        _model_router = ModelRouter()
    return _model_router