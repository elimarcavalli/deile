"""Self-Analyzer - Análise contínua de performance e identificação de melhorias"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from pathlib import Path
import json
import statistics
from enum import Enum

logger = logging.getLogger(__name__)


class ImprovementCategory(Enum):
    """Categorias de melhorias identificadas"""
    PERFORMANCE = "performance"
    CODE_QUALITY = "code_quality"
    USER_EXPERIENCE = "user_experience"
    FUNCTIONALITY = "functionality"
    RELIABILITY = "reliability"
    SCALABILITY = "scalability"


@dataclass
class PerformanceMetric:
    """Métrica de performance"""
    name: str
    value: float
    unit: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp


@dataclass
class ImprovementOpportunity:
    """Oportunidade de melhoria identificada"""
    opportunity_id: str
    category: ImprovementCategory
    description: str
    priority: int  # 1-10 (10 = crítico)
    impact_estimate: float  # Impacto estimado (0.0-1.0)
    effort_estimate: int  # Esforço estimado em horas
    confidence: float  # Confiança na análise (0.0-1.0)
    evidence: List[str] = field(default_factory=list)
    metrics_supporting: List[str] = field(default_factory=list)
    proposed_solution: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class SelfAnalyzer:
    """Analisa continuamente a performance do sistema e identifica melhorias

    Features:
    - Coleta automática de métricas de performance
    - Análise de padrões e tendências
    - Identificação de bottlenecks e problemas
    - Geração automática de oportunidades de melhoria
    - Priorização baseada em impacto/esforço
    """

    def __init__(self, metrics_dir: Path = None):
        self.metrics_dir = metrics_dir or Path("deile/evolution/metrics")
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        # Storage de métricas
        self._metrics: Dict[str, List[PerformanceMetric]] = {}
        self._improvement_opportunities: List[ImprovementOpportunity] = []

        # Configuração
        self.max_metrics_per_type = 1000
        self.analysis_interval = 300  # 5 minutos
        self.metric_retention_hours = 24

        # Tasks de análise
        self._analysis_task: Optional[asyncio.Task] = None
        self._is_running = False

        # Baselines para comparação
        self._baselines: Dict[str, float] = {}

        logger.info("SelfAnalyzer inicializado")

    async def start(self) -> None:
        """Inicia análise contínua"""
        if self._is_running:
            return

        self._is_running = True
        self._analysis_task = asyncio.create_task(self._analysis_loop())

        # Estabelece baselines iniciais
        await self._establish_baselines()

        logger.info("SelfAnalyzer iniciado - análise contínua ativa")

    async def stop(self) -> None:
        """Para análise contínua"""
        self._is_running = False

        if self._analysis_task:
            self._analysis_task.cancel()
            try:
                await self._analysis_task
            except asyncio.CancelledError:
                pass

        logger.info("SelfAnalyzer parado")

    async def record_metric(
        self,
        metric_name: str,
        value: float,
        unit: str = "",
        metadata: Dict[str, Any] = None
    ) -> None:
        """Registra uma métrica de performance"""
        metric = PerformanceMetric(
            name=metric_name,
            value=value,
            unit=unit,
            timestamp=time.time(),
            metadata=metadata or {}
        )

        if metric_name not in self._metrics:
            self._metrics[metric_name] = []

        self._metrics[metric_name].append(metric)

        # Limita número de métricas armazenadas
        if len(self._metrics[metric_name]) > self.max_metrics_per_type:
            self._metrics[metric_name] = self._metrics[metric_name][-self.max_metrics_per_type:]

        logger.debug(f"Métrica registrada: {metric_name} = {value} {unit}")

    async def get_current_metrics(self) -> Dict[str, Any]:
        """Retorna métricas atuais"""
        current = {}

        for metric_name, metrics in self._metrics.items():
            if not metrics:
                continue

            recent_metrics = [m for m in metrics if m.age_seconds < 3600]  # Última hora
            if not recent_metrics:
                continue

            values = [m.value for m in recent_metrics]
            current[metric_name] = {
                "current": values[-1] if values else 0,
                "average": statistics.mean(values),
                "min": min(values),
                "max": max(values),
                "count": len(values),
                "trend": self._calculate_trend(values)
            }

        return current

    async def analyze_performance(self) -> List[ImprovementOpportunity]:
        """Executa análise completa de performance"""
        logger.info("Iniciando análise de performance...")

        opportunities = []

        try:
            # Análise de tendências de performance
            performance_opportunities = await self._analyze_performance_trends()
            opportunities.extend(performance_opportunities)

            # Análise de uso de recursos
            resource_opportunities = await self._analyze_resource_usage()
            opportunities.extend(resource_opportunities)

            # Análise de padrões de erro
            error_opportunities = await self._analyze_error_patterns()
            opportunities.extend(error_opportunities)

            # Análise de tempo de resposta
            response_time_opportunities = await self._analyze_response_times()
            opportunities.extend(response_time_opportunities)

            # Prioriza oportunidades
            opportunities.sort(key=lambda o: o.priority * o.impact_estimate, reverse=True)

            # Armazena oportunidades
            self._improvement_opportunities.extend(opportunities)

            # Limita número de oportunidades armazenadas
            self._improvement_opportunities = self._improvement_opportunities[-100:]

            logger.info(f"Análise concluída: {len(opportunities)} oportunidades identificadas")
            return opportunities

        except Exception as e:
            logger.error(f"Erro na análise de performance: {e}")
            return []

    async def get_improvement_opportunities(
        self,
        category: Optional[ImprovementCategory] = None,
        min_priority: int = 1,
        max_results: int = 20
    ) -> List[ImprovementOpportunity]:
        """Retorna oportunidades de melhoria filtradas"""
        opportunities = self._improvement_opportunities

        # Filtra por categoria
        if category:
            opportunities = [o for o in opportunities if o.category == category]

        # Filtra por prioridade mínima
        opportunities = [o for o in opportunities if o.priority >= min_priority]

        # Ordena por prioridade e impacto
        opportunities.sort(key=lambda o: o.priority * o.impact_estimate, reverse=True)

        return opportunities[:max_results]

    async def _establish_baselines(self) -> None:
        """Estabelece baselines de performance"""
        # Baselines básicos - podem ser expandidos
        self._baselines = {
            "response_time_ms": 1000.0,
            "memory_usage_mb": 500.0,
            "cpu_usage_percent": 50.0,
            "error_rate_percent": 1.0,
            "tasks_per_minute": 10.0
        }

        logger.debug(f"Baselines estabelecidos: {self._baselines}")

    async def _analysis_loop(self) -> None:
        """Loop principal de análise"""
        logger.info("Loop de análise iniciado")

        while self._is_running:
            try:
                await asyncio.sleep(self.analysis_interval)

                # Limpa métricas antigas
                await self._cleanup_old_metrics()

                # Executa análise
                await self.analyze_performance()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erro no loop de análise: {e}")
                await asyncio.sleep(60)  # Aguarda antes de tentar novamente

        logger.info("Loop de análise finalizado")

    async def _analyze_performance_trends(self) -> List[ImprovementOpportunity]:
        """Analisa tendências de performance"""
        opportunities = []

        for metric_name, metrics in self._metrics.items():
            if len(metrics) < 10:  # Precisa de dados suficientes
                continue

            recent_values = [m.value for m in metrics[-20:]]
            trend = self._calculate_trend(recent_values)

            # Identifica tendências negativas
            if trend < -0.1 and metric_name.endswith(("_time", "_latency", "_duration")):
                opportunity = ImprovementOpportunity(
                    opportunity_id=f"perf_{metric_name}_{int(time.time())}",
                    category=ImprovementCategory.PERFORMANCE,
                    description=f"Degradação de performance detectada em {metric_name}",
                    priority=7,
                    impact_estimate=0.6,
                    effort_estimate=4,
                    confidence=0.8,
                    evidence=[f"Tendência negativa: {trend:.3f}"],
                    metrics_supporting=[metric_name],
                    proposed_solution=f"Investigar e otimizar {metric_name}"
                )
                opportunities.append(opportunity)

        return opportunities

    async def _analyze_resource_usage(self) -> List[ImprovementOpportunity]:
        """Analisa uso de recursos"""
        opportunities = []

        # Verifica uso de memória
        if "memory_usage_mb" in self._metrics:
            memory_metrics = self._metrics["memory_usage_mb"]
            if memory_metrics:
                current_memory = memory_metrics[-1].value
                baseline_memory = self._baselines.get("memory_usage_mb", 500.0)

                if current_memory > baseline_memory * 1.5:  # 50% acima do baseline
                    opportunity = ImprovementOpportunity(
                        opportunity_id=f"memory_{int(time.time())}",
                        category=ImprovementCategory.PERFORMANCE,
                        description="Alto uso de memória detectado",
                        priority=8,
                        impact_estimate=0.7,
                        effort_estimate=6,
                        confidence=0.9,
                        evidence=[f"Memória atual: {current_memory:.1f}MB (baseline: {baseline_memory:.1f}MB)"],
                        metrics_supporting=["memory_usage_mb"],
                        proposed_solution="Implementar otimizações de memória e garbage collection"
                    )
                    opportunities.append(opportunity)

        return opportunities

    async def _analyze_error_patterns(self) -> List[ImprovementOpportunity]:
        """Analisa padrões de erro"""
        opportunities = []

        if "error_count" in self._metrics:
            error_metrics = self._metrics["error_count"]
            recent_errors = [m for m in error_metrics if m.age_seconds < 3600]

            if len(recent_errors) > 10:  # Muitos erros na última hora
                opportunity = ImprovementOpportunity(
                    opportunity_id=f"errors_{int(time.time())}",
                    category=ImprovementCategory.RELIABILITY,
                    description="Alto número de erros detectado",
                    priority=9,
                    impact_estimate=0.8,
                    effort_estimate=8,
                    confidence=0.9,
                    evidence=[f"{len(recent_errors)} erros na última hora"],
                    metrics_supporting=["error_count"],
                    proposed_solution="Implementar melhor tratamento de erros e logging"
                )
                opportunities.append(opportunity)

        return opportunities

    async def _analyze_response_times(self) -> List[ImprovementOpportunity]:
        """Analisa tempos de resposta"""
        opportunities = []

        if "response_time_ms" in self._metrics:
            response_metrics = self._metrics["response_time_ms"]
            if len(response_metrics) >= 5:
                recent_times = [m.value for m in response_metrics[-20:]]
                avg_time = statistics.mean(recent_times)
                baseline_time = self._baselines.get("response_time_ms", 1000.0)

                if avg_time > baseline_time * 2:  # 100% mais lento que baseline
                    opportunity = ImprovementOpportunity(
                        opportunity_id=f"response_{int(time.time())}",
                        category=ImprovementCategory.PERFORMANCE,
                        description="Tempos de resposta elevados detectados",
                        priority=6,
                        impact_estimate=0.5,
                        effort_estimate=5,
                        confidence=0.7,
                        evidence=[f"Tempo médio: {avg_time:.1f}ms (baseline: {baseline_time:.1f}ms)"],
                        metrics_supporting=["response_time_ms"],
                        proposed_solution="Otimizar algoritmos e implementar caching"
                    )
                    opportunities.append(opportunity)

        return opportunities

    def _calculate_trend(self, values: List[float]) -> float:
        """Calcula tendência linear dos valores (-1 a 1)"""
        if len(values) < 2:
            return 0.0

        n = len(values)
        x_values = list(range(n))

        # Regressão linear simples
        sum_x = sum(x_values)
        sum_y = sum(values)
        sum_xy = sum(x * y for x, y in zip(x_values, values))
        sum_x2 = sum(x * x for x in x_values)

        slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x * sum_x)

        # Normaliza para -1 a 1
        max_value = max(values) if values else 1
        return min(1.0, max(-1.0, slope / max_value))

    async def _cleanup_old_metrics(self) -> None:
        """Remove métricas antigas"""
        cutoff_time = time.time() - (self.metric_retention_hours * 3600)

        for metric_name in self._metrics:
            self._metrics[metric_name] = [
                m for m in self._metrics[metric_name]
                if m.timestamp > cutoff_time
            ]

    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do analyzer"""
        total_metrics = sum(len(metrics) for metrics in self._metrics.values())

        return {
            "is_running": self._is_running,
            "total_metrics": total_metrics,
            "metric_types": len(self._metrics),
            "improvement_opportunities": len(self._improvement_opportunities),
            "baselines": self._baselines.copy(),
            "analysis_interval_seconds": self.analysis_interval
        }