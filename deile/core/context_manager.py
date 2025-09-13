"""Context Manager para gerenciamento de contexto e RAG - DEILE 2.0 ULTRA"""

from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from pathlib import Path
import asyncio
import json
import logging
import time
from collections import deque

from .exceptions import DEILEError, ValidationError
from ..parsers.base import ParseResult
from ..tools.base import ToolResult
from ..storage.embeddings import EmbeddingStore
from ..personas.manager import PersonaManager
from ..personas.instruction_loader import InstructionLoader
from ..memory.memory_manager import MemoryManager


logger = logging.getLogger(__name__)


@dataclass
class ContextChunk:
    """Chunk de contexto com metadata"""
    content: str
    source: str  # 'file', 'conversation', 'tool_result', etc.
    source_path: Optional[str] = None
    chunk_id: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    relevance_score: float = 0.0
    
    def __post_init__(self):
        if not self.chunk_id:
            self.chunk_id = f"{self.source}_{hash(self.content)}_{int(self.timestamp)}"


@dataclass
class ContextWindow:
    """Janela de contexto com limites de tokens"""
    chunks: List[ContextChunk] = field(default_factory=list)
    max_tokens: int = 8000  # Default para modelos médios
    current_tokens: int = 0
    
    def add_chunk(self, chunk: ContextChunk, estimated_tokens: int) -> bool:
        """Adiciona chunk se couber na janela"""
        if self.current_tokens + estimated_tokens <= self.max_tokens:
            self.chunks.append(chunk)
            self.current_tokens += estimated_tokens
            return True
        return False
    
    def remove_oldest(self) -> Optional[ContextChunk]:
        """Remove o chunk mais antigo"""
        if self.chunks:
            removed = self.chunks.pop(0)
            # Recalcula tokens (approximação)
            self.current_tokens = sum(len(c.content) // 4 for c in self.chunks)
            return removed
        return None
    
    def clear(self) -> None:
        """Limpa a janela"""
        self.chunks.clear()
        self.current_tokens = 0


class ContextManager:
    """Context Manager enterprise-grade para DEILE 2.0 ULTRA

    Integra novo sistema de personas e memory architecture híbrida:
    - PersonaManager para system instructions dinâmicas
    - MemoryManager para contexto inteligente
    - Event-driven context building
    - Hot-reload de configurações
    """

    def __init__(
        self,
        embedding_store: Optional[EmbeddingStore] = None,
        max_context_tokens: int = 8000,
        persona_manager: Optional[PersonaManager] = None,
        memory_manager: Optional[MemoryManager] = None
    ):
        # Core components
        self.embedding_store = embedding_store
        self.max_context_tokens = max_context_tokens

        # DEILE 2.0 ULTRA - Novos componentes
        self.persona_manager = persona_manager
        self.memory_manager = memory_manager

        # CORREÇÃO BG003: Instruction Loader para carregar de arquivos MD
        self.instruction_loader = InstructionLoader()

        # Estatísticas
        self._context_builds = 0
        self._persona_switches = 0
        self._memory_retrievals = 0
    
    async def build_context(
        self,
        user_input: str,
        parse_result: Optional[ParseResult] = None,
        tool_results: Optional[List[ToolResult]] = None,
        session: Optional[Any] = None,  # AgentSession
        **kwargs
    ) -> Dict[str, Any]:
        """Constrói contexto simplificado para Chat Sessions
        
        Chat Sessions gerenciam contexto automaticamente, então este método
        agora apenas prepara system instruction e informações básicas.
        """
        self._context_builds += 1
        start_time = time.time()
        
        try:
            # Prepara system instruction
            system_instruction = await self._build_system_instruction(
                parse_result, session, **kwargs
            )
            
            # Contexto base para Chat Sessions
            context = {
                "messages": [{"role": "user", "content": user_input}],
                "system_instruction": system_instruction,
                "metadata": {
                    "build_time": time.time() - start_time,
                    "user_input_length": len(user_input),
                    "tool_results_count": len(tool_results) if tool_results else 0,
                    "chat_session_mode": True
                }
            }

            # CORREÇÃO CRÍTICA: Inclui file_data se há arquivos uploadados no ParseResult
            if parse_result and parse_result.metadata and "uploaded_files" in parse_result.metadata:
                uploaded_files = parse_result.metadata["uploaded_files"]
                file_data_parts = []

                for file_info in uploaded_files:
                    if "file_data" in file_info:
                        file_data_parts.append(file_info["file_data"])

                if file_data_parts:
                    context["file_data_parts"] = file_data_parts
                    context["metadata"]["uploaded_files_count"] = len(file_data_parts)
                    logger.info(f"Added {len(file_data_parts)} file_data_parts to context")

            # Inclui file_data nas mensagens se disponível (para compatibilidade)
            if "file_data_parts" in context:
                # Modifica a mensagem do usuário para incluir file_data
                user_message = context["messages"][0]
                user_parts = [{"text": user_input}]

                # Adiciona file_data como parts adicionais
                for file_data in context["file_data_parts"]:
                    user_parts.append(file_data)

                user_message["parts"] = user_parts
                # Remove content para usar parts
                if "content" in user_message:
                    del user_message["content"]
            
            logger.debug(f"Built simplified context for chat session")
            return context
            
        except Exception as e:
            logger.error(f"Error building context: {e}")
            # Contexto mínimo de fallback
            return {
                "messages": [{"role": "user", "content": user_input}],
                "system_instruction": "You are DEILE, a helpful AI assistant.",
                "error": str(e)
            }
    
    def clear_cache(self) -> None:
        """Limpa todos os caches (mantido para compatibilidade)"""
        logger.debug("Cache clearing requested (simplified context manager)")
    
    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas simplificadas do context manager"""
        return {
            "context_builds": self._context_builds,
            "max_context_tokens": self.max_context_tokens,
            "chat_session_mode": True,
            "simplified": True
        }
    
    async def _build_system_instruction(
        self,
        parse_result: Optional[ParseResult],
        session: Optional[Any],
        **kwargs
    ) -> str:
        """Constrói instrução do sistema usando PersonaManager ou fallback hardcoded"""

        # CORREÇÃO CRÍTICA: Usa PersonaManager se disponível
        if self.persona_manager:
            try:
                active_persona = self.persona_manager.get_active_persona()
                if active_persona and active_persona.config.system_instruction:
                    logger.debug(f"Using persona '{active_persona.name}' system instruction")

                    # Usa instrução da persona ativa
                    base_instruction = active_persona.config.system_instruction

                    # Adiciona contexto de arquivos
                    file_context = await self._build_file_context(session, **kwargs)
                    if file_context:
                        base_instruction += f"\n\n📁 [ARQUIVOS DISPONÍVEIS NO PROJETO]\n{file_context}"

                    return base_instruction

            except Exception as e:
                logger.error(f"Error using PersonaManager: {e}, falling back to hardcoded")

        # Fallback para instrução de arquivo MD
        logger.debug("Using fallback system instruction from MD file (PersonaManager not available)")
        return await self._build_fallback_system_instruction(session, **kwargs)

    async def _build_fallback_system_instruction(
        self,
        session: Optional[Any],
        **kwargs
    ) -> str:
        """CORREÇÃO BG003: Carrega instrução de arquivo MD (não mais hardcoded!)"""

        logger.debug("Loading system instruction from MD file (fallback)")

        # Carrega instrução de arquivo MD
        base_instruction = self.instruction_loader.load_fallback_instruction()

        # Adiciona contexto de arquivos se disponível
        file_context = await self._build_file_context(session, **kwargs)
        if file_context:
            base_instruction += f"\n\n📁 [ARQUIVOS DISPONÍVEIS NO PROJETO]\n{file_context}"

        return base_instruction
    
    async def _build_file_context(self, session: Optional[Any], **kwargs) -> str:
        """Constrói contexto de arquivos disponíveis no projeto"""
        try:
            from pathlib import Path
            
            working_directory = None
            if session and hasattr(session, 'working_directory'):
                working_directory = session.working_directory
            elif 'working_directory' in kwargs:
                working_directory = kwargs['working_directory']
            
            if not working_directory:
                return ""
            
            work_dir = Path(working_directory)
            if not work_dir.exists():
                return ""
            
            # Lista arquivos principais do projeto
            file_list = []
            ignore_dirs = {'.git', '__pycache__', '.venv', 'venv', 'node_modules', 'logs', '.claude', 'cache'}
            
            import os

            # Procura recursivamente todos os arquivos e diretórios
            for root, dirs, files in os.walk(work_dir):
                for file in files:
                    if not file.startswith('.'):
                        file_list.append(f"{root / file}")
                for dir in dirs:
                    if dir not in ignore_dirs:
                        file_list.append(f"{root / dir}")
            
            # Adiciona informações úteis
            if file_list:
                file_list.append("💡 Você pode referenciar qualquer arquivo usando @nome_do_arquivo ou mencionar diretamente no texto.")
                file_list.append("💡 Use a ferramenta 'read_file' para ler qualquer arquivo que considerar pertinente ou mencionado pelo usuário.")
                return "\n".join(sorted(file_list))
            
            return ""
            
        except Exception as e:
            logger.debug(f"Error building file context: {e}")
            return ""
