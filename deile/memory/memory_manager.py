"""Gerenciador central da arquitetura híbrida de memória"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
import time
from pathlib import Path

from .working_memory import WorkingMemory
from .episodic_memory import EpisodicMemory
from .semantic_memory import SemanticMemory
from .procedural_memory import ProceduralMemory
from .memory_consolidation import MemoryConsolidator

logger = logging.getLogger(__name__)


@dataclass
class MemoryConfiguration:
    """Configuração do sistema de memória"""
    # Working Memory
    working_memory_size: int = 8000
    working_memory_ttl: int = 3600  # 1 hora

    # Episodic Memory
    max_episodes_per_session: int = 1000
    episode_retention_days: int = 30

    # Semantic Memory
    enable_vector_store: bool = True
    vector_dimensions: int = 768
    similarity_threshold: float = 0.7

    # Procedural Memory
    enable_pattern_learning: bool = True
    min_pattern_frequency: int = 3
    pattern_confidence_threshold: float = 0.8

    # Consolidation
    consolidation_interval: int = 3600  # 1 hora
    auto_cleanup_enabled: bool = True
    memory_pressure_threshold: float = 0.85  # 85%


class MemoryManager:
    """Orquestrador central da arquitetura híbrida de memória

    Coordena diferentes tipos de memória para fornecer contexto
    inteligente e aprendizado contínuo para o agente DEILE.

    Architecture:
    - Working Memory: Contexto imediato e cache ativo
    - Episodic Memory: Histórico de interações e sessões
    - Semantic Memory: Conhecimento estruturado e embeddings
    - Procedural Memory: Patterns e habilidades aprendidas
    """

    def __init__(self, config: MemoryConfiguration = None, memory_dir: Path = None):
        self.config = config or MemoryConfiguration()
        self.memory_dir = memory_dir or Path("deile/memory/storage")
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # Componentes de memória
        self.working_memory = WorkingMemory(
            max_size=self.config.working_memory_size,
            ttl=self.config.working_memory_ttl
        )

        self.episodic_memory = EpisodicMemory(
            storage_dir=self.memory_dir / "episodes",
            max_episodes_per_session=self.config.max_episodes_per_session,
            retention_days=self.config.episode_retention_days
        )

        self.semantic_memory = SemanticMemory(
            storage_dir=self.memory_dir / "semantic",
            enable_vector_store=self.config.enable_vector_store,
            vector_dimensions=self.config.vector_dimensions,
            similarity_threshold=self.config.similarity_threshold
        )

        self.procedural_memory = ProceduralMemory(
            storage_dir=self.memory_dir / "patterns",
            min_frequency=self.config.min_pattern_frequency,
            confidence_threshold=self.config.pattern_confidence_threshold
        )

        # Consolidator para otimização
        self.consolidator = MemoryConsolidator(
            working_memory=self.working_memory,
            episodic_memory=self.episodic_memory,
            semantic_memory=self.semantic_memory,
            procedural_memory=self.procedural_memory
        )

        # Estado do manager
        self._is_initialized = False
        self._consolidation_task: Optional[asyncio.Task] = None
        self._memory_stats = {
            "retrievals": 0,
            "stores": 0,
            "consolidations": 0,
            "last_consolidation": 0.0
        }

        logger.info("MemoryManager inicializado")

    async def initialize(self) -> None:
        """Inicializa todos os componentes de memória"""
        if self._is_initialized:
            return

        logger.info("Inicializando sistema de memória híbrida...")

        try:
            # Inicializa componentes individuais
            await self.working_memory.initialize()
            await self.episodic_memory.initialize()
            await self.semantic_memory.initialize()
            await self.procedural_memory.initialize()

            # Inicia processo de consolidação automática
            if self.config.consolidation_interval > 0:
                self._consolidation_task = asyncio.create_task(
                    self._consolidation_loop()
                )

            self._is_initialized = True
            logger.info("Sistema de memória inicializado com sucesso")

        except Exception as e:
            logger.error(f"Erro na inicialização do sistema de memória: {e}")
            raise

    async def store_interaction(
        self,
        user_input: str,
        agent_response: str,
        context: Dict[str, Any] = None,
        session_id: str = None
    ) -> str:
        """Armazena uma interação completa no sistema de memória

        Args:
            user_input: Input do usuário
            agent_response: Resposta do agente
            context: Contexto adicional
            session_id: ID da sessão

        Returns:
            str: ID da interação armazenada
        """
        if not self._is_initialized:
            await self.initialize()

        try:
            # Armazena na working memory para acesso imediato
            working_id = await self.working_memory.store_interaction(
                user_input, agent_response, context
            )

            # Armazena na episodic memory para histórico
            episode_id = await self.episodic_memory.store_episode(
                user_input, agent_response, context, session_id
            )

            # Extrai conhecimento para semantic memory (assíncrono)
            asyncio.create_task(self._extract_semantic_knowledge(
                user_input, agent_response, context
            ))

            # Analisa patterns para procedural memory (assíncrono)
            asyncio.create_task(self._analyze_interaction_patterns(
                user_input, agent_response, context
            ))

            self._memory_stats["stores"] += 1
            logger.debug(f"Interação armazenada: working_id={working_id}, episode_id={episode_id}")

            return episode_id

        except Exception as e:
            logger.error(f"Erro ao armazenar interação: {e}")
            raise

    async def retrieve_context(
        self,
        query: str,
        session_id: str = None,
        max_results: int = 10
    ) -> Dict[str, Any]:
        """Recupera contexto relevante de todos os tipos de memória

        Args:
            query: Query de busca
            session_id: ID da sessão atual
            max_results: Número máximo de resultados

        Returns:
            Dict com contexto organizado por tipo de memória
        """
        if not self._is_initialized:
            await self.initialize()

        try:
            # Busca paralela em todos os tipos de memória
            working_task = asyncio.create_task(
                self.working_memory.search(query, max_results)
            )
            episodic_task = asyncio.create_task(
                self.episodic_memory.search_episodes(query, session_id, max_results)
            )
            semantic_task = asyncio.create_task(
                self.semantic_memory.search_knowledge(query, max_results)
            )
            procedural_task = asyncio.create_task(
                self.procedural_memory.get_relevant_patterns(query)
            )

            # Aguarda todos os resultados
            working_results, episodic_results, semantic_results, procedural_results = \
                await asyncio.gather(working_task, episodic_task, semantic_task, procedural_task)

            context = {
                "working_memory": working_results,
                "episodic_memory": episodic_results,
                "semantic_memory": semantic_results,
                "procedural_memory": procedural_results,
                "metadata": {
                    "query": query,
                    "session_id": session_id,
                    "timestamp": time.time(),
                    "total_results": len(working_results) + len(episodic_results) +
                                   len(semantic_results) + len(procedural_results)
                }
            }

            self._memory_stats["retrievals"] += 1
            logger.debug(f"Contexto recuperado para query: '{query[:50]}...'")

            return context

        except Exception as e:
            logger.error(f"Erro na recuperação de contexto: {e}")
            # Retorna contexto mínimo em caso de erro
            return {"error": str(e), "working_memory": [], "episodic_memory": [],
                   "semantic_memory": [], "procedural_memory": []}

    async def learn_from_feedback(
        self,
        interaction_id: str,
        feedback_type: str,
        feedback_data: Dict[str, Any]
    ) -> None:
        """Aprende a partir de feedback do usuário

        Args:
            interaction_id: ID da interação
            feedback_type: Tipo de feedback ('positive', 'negative', 'correction')
            feedback_data: Dados específicos do feedback
        """
        try:
            # Atualiza working memory
            await self.working_memory.update_with_feedback(interaction_id, feedback_type, feedback_data)

            # Atualiza procedural memory com patterns de sucesso/falha
            await self.procedural_memory.update_pattern_effectiveness(
                interaction_id, feedback_type, feedback_data
            )

            # Se for correção, armazena na semantic memory
            if feedback_type == "correction":
                await self.semantic_memory.store_correction(interaction_id, feedback_data)

            logger.info(f"Feedback processado: {feedback_type} para interação {interaction_id}")

        except Exception as e:
            logger.error(f"Erro ao processar feedback: {e}")

    async def get_memory_usage(self) -> Dict[str, Any]:
        """Retorna informações sobre uso de memória"""
        if not self._is_initialized:
            return {"status": "not_initialized"}

        try:
            # Coleta estatísticas de cada componente
            working_stats = await self.working_memory.get_stats()
            episodic_stats = await self.episodic_memory.get_stats()
            semantic_stats = await self.semantic_memory.get_stats()
            procedural_stats = await self.procedural_memory.get_stats()

            # Calcula uso total estimado (em MB)
            total_memory_mb = (
                working_stats.get("memory_mb", 0) +
                episodic_stats.get("memory_mb", 0) +
                semantic_stats.get("memory_mb", 0) +
                procedural_stats.get("memory_mb", 0)
            )

            return {
                "total_memory_mb": total_memory_mb,
                "components": {
                    "working_memory": working_stats,
                    "episodic_memory": episodic_stats,
                    "semantic_memory": semantic_stats,
                    "procedural_memory": procedural_stats
                },
                "manager_stats": self._memory_stats.copy(),
                "consolidation_active": self._consolidation_task is not None and not self._consolidation_task.done()
            }

        except Exception as e:
            logger.error(f"Erro ao obter estatísticas de memória: {e}")
            return {"error": str(e)}

    async def optimize_memory(self, force: bool = False) -> Dict[str, Any]:
        """Executa otimização manual da memória

        Args:
            force: Se True, força consolidação mesmo se não necessária

        Returns:
            Relatório da otimização
        """
        try:
            logger.info("Iniciando otimização de memória...")

            # Executa consolidação
            consolidation_report = await self.consolidator.consolidate_all(force=force)

            # Atualiza estatísticas
            self._memory_stats["consolidations"] += 1
            self._memory_stats["last_consolidation"] = time.time()

            logger.info("Otimização de memória concluída")
            return consolidation_report

        except Exception as e:
            logger.error(f"Erro na otimização de memória: {e}")
            return {"error": str(e)}

    async def _extract_semantic_knowledge(
        self,
        user_input: str,
        agent_response: str,
        context: Dict[str, Any]
    ) -> None:
        """Extrai conhecimento semântico de uma interação"""
        try:
            # Identifica entidades, conceitos e relações
            # Por simplicidade, armazenamos como-é - pode ser expandido com NLP
            knowledge = {
                "user_input": user_input,
                "agent_response": agent_response,
                "context": context,
                "extracted_at": time.time()
            }

            await self.semantic_memory.store_knowledge(knowledge)

        except Exception as e:
            logger.error(f"Erro na extração de conhecimento semântico: {e}")

    async def _analyze_interaction_patterns(
        self,
        user_input: str,
        agent_response: str,
        context: Dict[str, Any]
    ) -> None:
        """Analisa patterns na interação"""
        try:
            # Identifica patterns de entrada/saída
            pattern_data = {
                "input_length": len(user_input),
                "output_length": len(agent_response),
                "context_keys": list(context.keys()) if context else [],
                "timestamp": time.time()
            }

            await self.procedural_memory.analyze_interaction(pattern_data)

        except Exception as e:
            logger.error(f"Erro na análise de patterns: {e}")

    async def _consolidation_loop(self) -> None:
        """Loop de consolidação automática executado em background"""
        logger.info("Iniciando loop de consolidação automática")

        while True:
            try:
                await asyncio.sleep(self.config.consolidation_interval)

                # Verifica pressão de memória
                memory_stats = await self.get_memory_usage()
                total_memory = memory_stats.get("total_memory_mb", 0)

                # Se memória está alta, força consolidação
                if total_memory > 1000 * self.config.memory_pressure_threshold:  # 850MB default
                    logger.info(f"Pressão de memória detectada ({total_memory}MB), iniciando consolidação")
                    await self.optimize_memory(force=True)
                else:
                    # Consolidação normal
                    await self.optimize_memory(force=False)

            except asyncio.CancelledError:
                logger.info("Loop de consolidação cancelado")
                break
            except Exception as e:
                logger.error(f"Erro no loop de consolidação: {e}")
                # Continua o loop mesmo com erro
                await asyncio.sleep(60)  # Aguarda 1 minuto antes de tentar novamente

    async def shutdown(self) -> None:
        """Finaliza o sistema de memória gracefully"""
        logger.info("Finalizando sistema de memória...")

        try:
            # Cancela loop de consolidação
            if self._consolidation_task:
                self._consolidation_task.cancel()
                try:
                    await self._consolidation_task
                except asyncio.CancelledError:
                    pass

            # Executa consolidação final
            await self.optimize_memory(force=True)

            # Finaliza componentes
            await self.working_memory.shutdown()
            await self.episodic_memory.shutdown()
            await self.semantic_memory.shutdown()
            await self.procedural_memory.shutdown()

            self._is_initialized = False
            logger.info("Sistema de memória finalizado")

        except Exception as e:
            logger.error(f"Erro na finalização do sistema de memória: {e}")