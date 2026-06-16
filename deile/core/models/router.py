"""Model Router para seleção inteligente de modelos"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..exceptions import ModelError
from .base import ModelProvider
from .routing_strategies import (
    ModelMetrics,
    RoutingContext,
    RoutingStrategy,
    RoutingStrategySelector,
)
from .tier import ModelTier
from .tier_router import get_tier_router

logger = logging.getLogger(__name__)


class ModelRouter:
    """Roteador inteligente de modelos de IA

    Responsável por:
    - Seleção automática do melhor modelo para cada tarefa
    - Balanceamento de carga entre modelos
    - Otimização de custo e performance
    - Fallback automático em caso de falha
    """

    def __init__(
        self,
        default_strategy: RoutingStrategy = RoutingStrategy.TASK_OPTIMIZED,
    ):
        self.providers: Dict[str, ModelProvider] = {}
        self.metrics: Dict[str, ModelMetrics] = {}
        self.strategy = default_strategy

        # Configurações
        self.fallback_enabled = True
        self.health_check_interval = 300  # 5 minutos
        self.circuit_breaker_enabled = True
        self.circuit_breaker_threshold = 0.8  # 80% de erro para abrir circuito

        # Estado interno
        self._circuit_breaker_status: Dict[str, bool] = {}  # True = aberto (bloqueado)
        self._last_health_check = 0

        # Máquina de estratégias legada (fallback quando não há tier ou o
        # TierRouter falha) — ver routing_strategies.py.
        self._selector = RoutingStrategySelector()

        # Funções de decisão customizáveis
        self.custom_routing_functions: List[
            Callable[[RoutingContext, List[ModelProvider]], Optional[ModelProvider]]
        ] = []

        # (f"ModelRouter initialized with strategy: {default_strategy.value}")

    def register_provider(
        self, provider: ModelProvider, priority: int = 0, cost_per_token: float = 0.0
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
        routing_context: Optional[RoutingContext] = None,
        tier: Optional[ModelTier] = None,
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

        # Tier-aware routing: delegate to TierRouter when a tier is specified
        if tier is not None:
            try:
                tier_router = get_tier_router()
                # Register any providers that aren't yet known to the TierRouter
                for provider in self.providers.values():
                    if provider.provider_id not in tier_router.registered_providers():
                        tier_router.register_provider(provider)
                selected = tier_router.select(tier)
                logger.debug(
                    "TierRouter selected provider_id=%s for tier=%s",
                    selected.provider_id,
                    tier.value,
                )
                return selected
            except Exception as exc:
                logger.warning(
                    "TierRouter.select failed (%s), falling back to legacy routing", exc
                )
                # Fall through to legacy routing below

        # Obtém provedores disponíveis
        available_providers = await self._get_available_providers()

        # Debug logging
        logger.debug(f"Total providers: {len(self.providers)}")
        logger.debug(f"Available providers: {len(available_providers)}")
        for key, provider in self.providers.items():
            logger.debug(
                f"Provider {key}: circuit_breaker={self._circuit_breaker_status.get(key, False)}"
            )

        if not available_providers:
            raise ModelError(
                f"No model providers available. Total: {len(self.providers)}, Available: {len(available_providers)}",
                error_code="NO_PROVIDERS",
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
        selected_provider = self._selector.select(
            self.strategy, routing_context, available_providers, self.metrics
        )

        if not selected_provider:
            # Fallback: primeiro provedor disponível
            selected_provider = available_providers[0]
            logger.warning("Using fallback provider selection")

        provider_key = (
            f"{selected_provider.provider_name}:{selected_provider.model_name}"
        )
        self.metrics[provider_key].record_request()

        logger.debug(
            f"Selected provider: {provider_key} (strategy: {self.strategy.value})"
        )
        return selected_provider

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
                "last_used": metrics.last_used,
            }

        return {
            "strategy": self.strategy.value,
            "total_providers": len(self.providers),
            "available_providers": len(await self._get_available_providers()),
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "fallback_enabled": self.fallback_enabled,
            "provider_stats": provider_stats,
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
        self, context: Optional[Dict[str, Any]], session: Optional[Any]
    ) -> RoutingContext:
        """Cria contexto de roteamento a partir do contexto geral"""
        user_input = ""
        estimated_tokens = 0

        if context:
            if isinstance(context, dict):
                messages = context.get("messages", [])
                if messages:
                    last_message = messages[-1]
                    if hasattr(last_message, "content"):
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
            session_data=(
                session.context_data
                if session and hasattr(session, "context_data")
                else {}
            ),
        )

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
