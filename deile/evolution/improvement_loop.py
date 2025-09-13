"""Improvement Loop - Ciclo contínuo de auto-melhoria"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from pathlib import Path
import uuid

from .self_analyzer import SelfAnalyzer, ImprovementOpportunity
from .code_modifier import CodeModifier
from .benchmarker import Benchmarker
from .safety_sandbox import SafetySandbox
from .rollback_manager import RollbackManager

logger = logging.getLogger(__name__)


@dataclass
class ImprovementAttempt:
    """Tentativa de melhoria"""
    attempt_id: str
    opportunity: ImprovementOpportunity
    modification_plan: Dict[str, Any]
    started_at: float
    completed_at: Optional[float] = None
    success: bool = False
    performance_before: Dict[str, float] = None
    performance_after: Dict[str, float] = None
    rollback_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class ImprovementLoop:
    """Ciclo contínuo de auto-melhoria baseado nas práticas de 2025

    Implementa o ciclo completo:
    1. Identifica oportunidades (via SelfAnalyzer)
    2. Planeja modificações (via CodeModifier)
    3. Testa em sandbox (via SafetySandbox)
    4. Valida melhorias (via Benchmarker)
    5. Aplica ou reverte (via RollbackManager)
    6. Monitora resultados e aprende
    """

    def __init__(
        self,
        self_analyzer: SelfAnalyzer,
        code_modifier: Optional[CodeModifier] = None,
        benchmarker: Optional[Benchmarker] = None,
        safety_sandbox: Optional[SafetySandbox] = None,
        rollback_manager: Optional[RollbackManager] = None
    ):
        self.self_analyzer = self_analyzer
        self.code_modifier = code_modifier or CodeModifier()
        self.benchmarker = benchmarker or Benchmarker()
        self.safety_sandbox = safety_sandbox or SafetySandbox()
        self.rollback_manager = rollback_manager or RollbackManager()

        # Configuração do loop
        self.loop_interval = 1800  # 30 minutos
        self.max_concurrent_improvements = 2
        self.min_confidence_threshold = 0.7
        self.max_improvement_time = 1800  # 30 minutos por tentativa

        # Estado do loop
        self._is_running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._active_attempts: Dict[str, ImprovementAttempt] = {}
        self._completed_attempts: List[ImprovementAttempt] = []

        # Métricas do loop
        self._stats = {
            "total_attempts": 0,
            "successful_improvements": 0,
            "failed_improvements": 0,
            "rollbacks_performed": 0,
            "average_improvement_time": 0.0,
            "last_improvement": 0.0
        }

        logger.info("ImprovementLoop inicializado")

    async def start(self) -> None:
        """Inicia o ciclo de melhoria contínua"""
        if self._is_running:
            return

        logger.info("Iniciando ciclo de melhoria contínua...")

        # Inicializa componentes
        await self.self_analyzer.start()
        await self.code_modifier.initialize()
        await self.benchmarker.initialize()
        await self.safety_sandbox.initialize()
        await self.rollback_manager.initialize()

        self._is_running = True
        self._loop_task = asyncio.create_task(self._improvement_loop())

        logger.info("Ciclo de melhoria contínua iniciado")

    async def stop(self) -> None:
        """Para o ciclo de melhoria"""
        if not self._is_running:
            return

        logger.info("Parando ciclo de melhoria...")

        self._is_running = False

        # Para loop principal
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

        # Cancela tentativas ativas
        for attempt in self._active_attempts.values():
            logger.warning(f"Cancelando tentativa ativa: {attempt.attempt_id}")

        # Para componentes
        await self.self_analyzer.stop()
        await self.code_modifier.shutdown()
        await self.benchmarker.shutdown()
        await self.safety_sandbox.shutdown()
        await self.rollback_manager.shutdown()

        logger.info("Ciclo de melhoria parado")

    async def request_immediate_improvement(
        self,
        category: Optional[str] = None,
        max_attempts: int = 3
    ) -> List[ImprovementAttempt]:
        """Solicita melhoria imediata (fora do ciclo automático)"""
        logger.info(f"Solicitação de melhoria imediata (categoria: {category})")

        opportunities = await self.self_analyzer.get_improvement_opportunities(
            category=category,
            min_priority=5,  # Apenas prioridades altas
            max_results=max_attempts
        )

        attempts = []
        for opportunity in opportunities:
            if len(self._active_attempts) >= self.max_concurrent_improvements:
                logger.warning("Máximo de melhorias simultâneas atingido")
                break

            attempt = await self._execute_improvement(opportunity)
            if attempt:
                attempts.append(attempt)

        return attempts

    async def _improvement_loop(self) -> None:
        """Loop principal de melhoria contínua"""
        logger.info("Loop de melhoria iniciado")

        while self._is_running:
            try:
                await asyncio.sleep(self.loop_interval)

                # Verifica se já não há muitas melhorias ativas
                if len(self._active_attempts) >= self.max_concurrent_improvements:
                    logger.debug("Máximo de melhorias simultâneas atingido, aguardando...")
                    continue

                # Obtém oportunidades de melhoria
                opportunities = await self.self_analyzer.get_improvement_opportunities(
                    min_priority=3,
                    max_results=5
                )

                if not opportunities:
                    logger.debug("Nenhuma oportunidade de melhoria encontrada")
                    continue

                # Seleciona melhor oportunidade
                best_opportunity = self._select_best_opportunity(opportunities)
                if not best_opportunity:
                    continue

                # Executa melhoria
                await self._execute_improvement(best_opportunity)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Erro no loop de melhoria: {e}")
                await asyncio.sleep(300)  # Aguarda 5 minutos antes de tentar novamente

        logger.info("Loop de melhoria finalizado")

    def _select_best_opportunity(self, opportunities: List[ImprovementOpportunity]) -> Optional[ImprovementOpportunity]:
        """Seleciona a melhor oportunidade de melhoria"""
        if not opportunities:
            return None

        # Filtra por confiança mínima
        viable_opportunities = [
            o for o in opportunities
            if o.confidence >= self.min_confidence_threshold
        ]

        if not viable_opportunities:
            return None

        # Calcula score baseado em prioridade, impacto e esforço
        def calculate_score(opp: ImprovementOpportunity) -> float:
            # Score = (Prioridade * Impacto) / (Esforço^0.5)
            effort_factor = max(1, opp.effort_estimate) ** 0.5
            return (opp.priority * opp.impact_estimate * opp.confidence) / effort_factor

        best_opportunity = max(viable_opportunities, key=calculate_score)

        logger.info(f"Oportunidade selecionada: {best_opportunity.description} "
                   f"(prioridade: {best_opportunity.priority}, impacto: {best_opportunity.impact_estimate})")

        return best_opportunity

    async def _execute_improvement(self, opportunity: ImprovementOpportunity) -> Optional[ImprovementAttempt]:
        """Executa uma tentativa de melhoria completa"""
        attempt_id = str(uuid.uuid4())[:8]

        logger.info(f"Iniciando tentativa de melhoria: {attempt_id} - {opportunity.description}")

        attempt = ImprovementAttempt(
            attempt_id=attempt_id,
            opportunity=opportunity,
            modification_plan={},
            started_at=time.time()
        )

        self._active_attempts[attempt_id] = attempt
        self._stats["total_attempts"] += 1

        try:
            # 1. Mede performance antes da melhoria
            attempt.performance_before = await self.benchmarker.measure_current_performance()

            # 2. Gera plano de modificação
            modification_plan = await self.code_modifier.generate_improvement_plan(opportunity)
            attempt.modification_plan = modification_plan

            if not modification_plan.get("feasible", False):
                raise Exception("Plano de modificação não é viável")

            # 3. Executa modificação em sandbox
            sandbox_result = await self.safety_sandbox.test_modification(modification_plan)

            if not sandbox_result.get("success", False):
                raise Exception(f"Teste em sandbox falhou: {sandbox_result.get('error')}")

            # 4. Cria ponto de rollback
            rollback_data = await self.rollback_manager.create_rollback_point(
                f"improvement_{attempt_id}"
            )
            attempt.rollback_data = rollback_data

            # 5. Aplica modificação no sistema real
            application_result = await self.code_modifier.apply_modification(modification_plan)

            if not application_result.get("success", False):
                raise Exception(f"Aplicação da modificação falhou: {application_result.get('error')}")

            # 6. Aguarda estabilização e mede performance
            await asyncio.sleep(30)  # Aguarda estabilização
            attempt.performance_after = await self.benchmarker.measure_current_performance()

            # 7. Valida melhoria
            improvement_validated = await self._validate_improvement(attempt)

            if improvement_validated:
                # Sucesso - confirma mudanças
                attempt.success = True
                attempt.completed_at = time.time()
                self._stats["successful_improvements"] += 1
                self._stats["last_improvement"] = time.time()

                logger.info(f"Melhoria aplicada com sucesso: {attempt_id}")

                # Remove ponto de rollback após confirmação
                asyncio.create_task(
                    self.rollback_manager.remove_rollback_point(rollback_data["rollback_id"])
                )

            else:
                # Falhou validação - executa rollback
                await self._rollback_improvement(attempt)
                raise Exception("Melhoria não validada - rollback executado")

        except Exception as e:
            # Erro durante execução
            attempt.success = False
            attempt.error_message = str(e)
            attempt.completed_at = time.time()
            self._stats["failed_improvements"] += 1

            logger.error(f"Tentativa de melhoria falhou: {attempt_id} - {e}")

            # Executa rollback se necessário
            if attempt.rollback_data:
                await self._rollback_improvement(attempt)

        finally:
            # Move de ativo para completado
            if attempt_id in self._active_attempts:
                del self._active_attempts[attempt_id]

            self._completed_attempts.append(attempt)

            # Limita histórico
            if len(self._completed_attempts) > 100:
                self._completed_attempts = self._completed_attempts[-100:]

            # Atualiza estatísticas
            if attempt.completed_at:
                duration = attempt.completed_at - attempt.started_at
                total_time = self._stats["average_improvement_time"] * self._stats["total_attempts"]
                self._stats["average_improvement_time"] = (total_time + duration) / self._stats["total_attempts"]

        return attempt

    async def _validate_improvement(self, attempt: ImprovementAttempt) -> bool:
        """Valida se a melhoria foi efetiva"""
        if not attempt.performance_after or not attempt.performance_before:
            return False

        # Verifica métricas específicas baseadas na categoria da oportunidade
        opportunity = attempt.opportunity

        if opportunity.category.value == "performance":
            # Para melhorias de performance, verifica redução em métricas de tempo
            time_metrics = ["response_time_ms", "processing_time_ms", "execution_time_ms"]

            for metric in time_metrics:
                before = attempt.performance_before.get(metric)
                after = attempt.performance_after.get(metric)

                if before and after:
                    improvement_percent = (before - after) / before * 100

                    if improvement_percent > 5:  # Melhoria de pelo menos 5%
                        logger.info(f"Melhoria validada em {metric}: {improvement_percent:.1f}%")
                        return True

        elif opportunity.category.value == "reliability":
            # Para melhorias de confiabilidade, verifica redução de erros
            error_metrics = ["error_count", "failure_rate", "exception_count"]

            for metric in error_metrics:
                before = attempt.performance_before.get(metric, 0)
                after = attempt.performance_after.get(metric, 0)

                if after < before:  # Redução de erros
                    logger.info(f"Melhoria validada em {metric}: {before} -> {after}")
                    return True

        # Validação geral - verifica se não houve degradação significativa
        critical_metrics = ["response_time_ms", "memory_usage_mb", "error_count"]
        degraded_metrics = []

        for metric in critical_metrics:
            before = attempt.performance_before.get(metric)
            after = attempt.performance_after.get(metric)

            if before and after:
                if metric == "error_count":
                    # Erros não devem aumentar significativamente
                    if after > before * 1.2:  # 20% mais erros
                        degraded_metrics.append(metric)
                else:
                    # Outras métricas não devem piorar muito
                    if after > before * 1.3:  # 30% pior
                        degraded_metrics.append(metric)

        if degraded_metrics:
            logger.warning(f"Métricas degradadas após melhoria: {degraded_metrics}")
            return False

        # Se chegou até aqui, considera válida (pelo menos não degradou)
        return True

    async def _rollback_improvement(self, attempt: ImprovementAttempt) -> None:
        """Executa rollback de uma melhoria"""
        if not attempt.rollback_data:
            logger.warning(f"Nenhum ponto de rollback disponível para {attempt.attempt_id}")
            return

        try:
            rollback_result = await self.rollback_manager.rollback_to_point(
                attempt.rollback_data["rollback_id"]
            )

            if rollback_result.get("success", False):
                logger.info(f"Rollback executado com sucesso: {attempt.attempt_id}")
                self._stats["rollbacks_performed"] += 1
            else:
                logger.error(f"Falha no rollback: {rollback_result.get('error')}")

        except Exception as e:
            logger.error(f"Erro durante rollback: {e}")

    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do improvement loop"""
        return {
            "is_running": self._is_running,
            "active_attempts": len(self._active_attempts),
            "completed_attempts": len(self._completed_attempts),
            "loop_interval_seconds": self.loop_interval,
            "max_concurrent_improvements": self.max_concurrent_improvements,
            "stats": self._stats.copy()
        }

    async def get_recent_attempts(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Retorna tentativas recentes"""
        recent = self._completed_attempts[-limit:] if self._completed_attempts else []

        return [
            {
                "attempt_id": attempt.attempt_id,
                "opportunity_description": attempt.opportunity.description,
                "success": attempt.success,
                "duration_seconds": (attempt.completed_at - attempt.started_at) if attempt.completed_at else None,
                "error_message": attempt.error_message,
                "started_at": attempt.started_at
            }
            for attempt in recent
        ]