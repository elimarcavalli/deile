"""Legacy strategy-based provider selection for :class:`ModelRouter`.

``ModelRouter`` delegates tier-aware selection to ``TierRouter``; this module
holds the older strategy machine (round-robin / least-busy / task- / cost- /
performance-optimized / load-balanced) used only as the no-tier or
TierRouter-failure fallback. Keeping it here lets ``ModelRouter`` stay focused
on its own responsibility — provider registration, metrics and circuit
breaking.

Provider-agnostic: this module must NOT import any external SDK.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from deile.core.models.base import ModelProvider, ModelSize, ModelType

logger = logging.getLogger(__name__)


class RoutingStrategy(Enum):
    """Estratégias de roteamento de modelos"""

    ROUND_ROBIN = "round_robin"  # Alternância simples
    LEAST_BUSY = "least_busy"  # Modelo menos ocupado
    TASK_OPTIMIZED = "task_optimized"  # Otimizado para o tipo de tarefa
    COST_OPTIMIZED = "cost_optimized"  # Otimizado para menor custo
    PERFORMANCE_OPTIMIZED = "performance"  # Otimizado para performance
    LOAD_BALANCED = "load_balanced"  # Balanceamento de carga


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
    """Métricas de um modelo.

    ``active_requests`` is kept for backwards compatibility but is never
    incremented — a correct "active" counter would need symmetric
    ``record_complete`` calls at every agent call site (refactor out of
    scope). ``LEAST_BUSY``/``LOAD_BALANCED`` now use ``total_requests`` as
    a monotonic proxy for "least historically used".
    """

    total_requests: int = 0
    active_requests: int = 0  # deprecated — always 0; see class docstring
    avg_response_time: float = 0.0
    error_rate: float = 0.0
    cost_per_token: float = 0.0
    success_rate: float = 1.0
    last_used: float = 0.0

    def record_request(self) -> None:
        """Registra nova requisição."""
        self.total_requests += 1
        self.last_used = time.time()


def _provider_key(provider: ModelProvider) -> str:
    return f"{provider.provider_name}:{provider.model_name}"


class RoutingStrategySelector:
    """Applies a :class:`RoutingStrategy` to pick a provider from candidates.

    Owns the round-robin cursor and the task→size mapping. Per-provider
    metrics are passed in on each call because they are owned and mutated by
    :class:`ModelRouter`.
    """

    def __init__(self) -> None:
        # ``itertools.count`` is atomic under the CPython GIL — replaces a
        # non-atomic ``idx = self._cursor; self._cursor += 1`` that allowed
        # two concurrent ``select_provider`` callers to collide on the same
        # provider when an ``await`` was interleaved between the load+write.
        from itertools import count

        self._round_robin_counter = count()
        # Mapeamento de tarefas para tipos de modelo
        self.task_model_mapping = {
            "code_analysis": ModelSize.MEDIUM,
            "code_generation": ModelSize.LARGE,
            "file_summary": ModelSize.SMALL,
            "complex_reasoning": ModelSize.LARGE,
            "simple_questions": ModelSize.SMALL,
            "translation": ModelSize.MEDIUM,
            "embedding": ModelType.EMBEDDING,
        }

    def select(
        self,
        strategy: RoutingStrategy,
        context: RoutingContext,
        providers: List[ModelProvider],
        metrics: Dict[str, ModelMetrics],
    ) -> Optional[ModelProvider]:
        """Aplica a estratégia de roteamento selecionada."""
        if strategy == RoutingStrategy.ROUND_ROBIN:
            return self._round_robin_selection(providers)
        elif strategy == RoutingStrategy.LEAST_BUSY:
            return self._least_busy_selection(providers, metrics)
        elif strategy == RoutingStrategy.TASK_OPTIMIZED:
            return self._task_optimized_selection(context, providers, metrics)
        elif strategy == RoutingStrategy.COST_OPTIMIZED:
            return self._cost_optimized_selection(context, providers, metrics)
        elif strategy == RoutingStrategy.PERFORMANCE_OPTIMIZED:
            return self._performance_optimized_selection(providers, metrics)
        elif strategy == RoutingStrategy.LOAD_BALANCED:
            return self._load_balanced_selection(providers, metrics)
        else:
            logger.warning("Unknown routing strategy: %s", strategy)
            return providers[0] if providers else None

    def _round_robin_selection(
        self, providers: List[ModelProvider]
    ) -> Optional[ModelProvider]:
        """Seleção round-robin"""
        if not providers:
            return None

        idx = next(self._round_robin_counter)
        return providers[idx % len(providers)]

    def _least_busy_selection(
        self, providers: List[ModelProvider], metrics: Dict[str, ModelMetrics]
    ) -> Optional[ModelProvider]:
        """Seleciona provedor menos usado historicamente (proxy:
        ``total_requests`` — monotonic, correct; ``active_requests`` was
        a broken never-decremented counter)."""
        if not providers:
            return None

        return min(
            providers,
            key=lambda p: metrics[_provider_key(p)].total_requests,
        )

    def _task_optimized_selection(
        self,
        context: RoutingContext,
        providers: List[ModelProvider],
        metrics: Dict[str, ModelMetrics],
    ) -> Optional[ModelProvider]:
        """Seleciona provedor otimizado para a tarefa"""
        if not providers:
            return None

        # Identifica tipo de tarefa baseado no input
        task_type = self._identify_task_type(context.user_input)
        preferred_size = self.task_model_mapping.get(task_type, ModelSize.MEDIUM)

        # Filtra provedores pelo tamanho preferido
        preferred_providers = [p for p in providers if p.model_size == preferred_size]

        if preferred_providers:
            # Seleciona o com melhor taxa de sucesso entre os preferidos
            return max(
                preferred_providers,
                key=lambda p: metrics[_provider_key(p)].success_rate,
            )

        # Fallback: melhor provedor geral
        return max(providers, key=lambda p: metrics[_provider_key(p)].success_rate)

    def _cost_optimized_selection(
        self,
        context: RoutingContext,
        providers: List[ModelProvider],
        metrics: Dict[str, ModelMetrics],
    ) -> Optional[ModelProvider]:
        """Seleciona provedor com melhor custo-benefício"""
        if not providers:
            return None

        # Calcula custo estimado para cada provedor
        def calculate_cost_score(provider: ModelProvider) -> float:
            m = metrics[_provider_key(provider)]

            # Custo = cost_per_token * estimated_tokens / success_rate
            # (penaliza provedores com alta taxa de erro)
            if m.success_rate == 0:
                return float("inf")

            cost = (m.cost_per_token * context.estimated_tokens) / m.success_rate
            return cost

        return min(providers, key=calculate_cost_score)

    def _performance_optimized_selection(
        self, providers: List[ModelProvider], metrics: Dict[str, ModelMetrics]
    ) -> Optional[ModelProvider]:
        """Seleciona provedor com melhor performance"""
        if not providers:
            return None

        # Score baseado em tempo de resposta e taxa de sucesso
        def performance_score(provider: ModelProvider) -> float:
            m = metrics[_provider_key(provider)]

            if m.avg_response_time == 0 or m.success_rate == 0:
                return 0

            # Score = success_rate / response_time (maior = melhor)
            return m.success_rate / m.avg_response_time

        return max(providers, key=performance_score)

    def _load_balanced_selection(
        self, providers: List[ModelProvider], metrics: Dict[str, ModelMetrics]
    ) -> Optional[ModelProvider]:
        """Seleção com balanceamento de carga"""
        if not providers:
            return None

        # Combine least_busy with performance.
        # Uses ``total_requests`` (see ``ModelMetrics`` docstring).
        def load_score(provider: ModelProvider) -> float:
            m = metrics[_provider_key(provider)]

            # Score inverso: menor = melhor
            load_factor = m.total_requests + 1
            performance_factor = m.avg_response_time if m.avg_response_time > 0 else 1

            return load_factor * performance_factor / max(m.success_rate, 0.1)

        return min(providers, key=load_score)

    def _identify_task_type(self, user_input: str) -> str:
        """Identifica tipo de tarefa baseado na entrada do usuário"""
        input_lower = user_input.lower()

        # Análise de padrões simples - pode ser refinada
        if any(
            word in input_lower for word in ["analyze", "review", "check", "examine"]
        ):
            return "code_analysis"
        elif any(
            word in input_lower for word in ["create", "generate", "write", "implement"]
        ):
            return "code_generation"
        elif any(word in input_lower for word in ["summarize", "explain", "what is"]):
            return "file_summary"
        elif len(user_input) > 500 or any(
            word in input_lower for word in ["complex", "detailed", "comprehensive"]
        ):
            return "complex_reasoning"
        else:
            return "simple_questions"
