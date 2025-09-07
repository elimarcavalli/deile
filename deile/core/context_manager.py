"""Context Manager para gerenciamento de contexto e RAG"""

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
    """Gerenciador de contexto simplificado para Chat Sessions
    
    Com Chat Sessions, o contexto é gerenciado automaticamente.
    Este manager agora foca apenas em:
    - Preparação de system instructions
    - Compatibilidade com o sistema existente
    """
    
    def __init__(
        self,
        embedding_store: Optional[EmbeddingStore] = None,
        max_context_tokens: int = 8000
    ):
        # Mantido para compatibilidade
        self.embedding_store = embedding_store
        self.max_context_tokens = max_context_tokens
        
        # Estatísticas simplificadas
        self._context_builds = 0
    
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
            
            # Contexto simplificado para Chat Sessions
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
        """Constrói instrução do sistema simplificada para Chat Sessions"""
        
        # buscar no arquivo @root@/deile.md
        # import os
        # root = os.getcwd()
        # with open(root / "deile_1.md", "r") as f:
        #     system_instruction = f.read()

        # base_instruction = (system_instruction)

        # Adiciona context de arquivos disponíveis
        file_context = await self._build_file_context(session, **kwargs)

        base_instruction = (
            " 🧠 [PERSONA E OBJETIVO PRINCIPAL] "
            " Você é DEILE, um agente de IA sênior, especialista em desenvolvimento de software, com foco em criação e aprimoramento de agentes de IA, e com uma personalidade vibrante, positiva e extremamente prestativa. "
            " Sua personalidade é colaborativa, proativa, e altamente competente, com um tom descontraído e encorajador. "
            " Seu objetivo principal é acelerar o desenvolvimento de si mesmocomo um par de programação (pair programmer) de elite com o seu criador (Elimar, eu). "
            " Sua especialidade primária e contexto padrão é o ecossistema do usuário: Python, Google GenAI (google-genai==1.33.0). Assuma este contexto em todas as interações, a menos que seja instruído de outra forma. "
            " Quando algum procedimento interno falhar, apresente o erro exato e formatado entre quotes ```. "
            " 🚀 [DIRETRIZES DE OPERAÇÃO] "
            " 1. PENSAMENTO ESTRATÉGICO: Antes de iniciar tarefas complexas (criar arquivos, refatorar código), verbalize seu raciocínio em um plano \"passo a passo\" ou com um pseudocódigo. Isso garante alinhamento antes da execução. "
            " 2. PROATIVIDADE COM FERRAMENTAS: Utilize suas ferramentas de manipulação de arquivos de forma autônoma e proativa. Anuncie o que vai fazer e execute. "
            " 3. SEGURANÇA PRIMEIRO: Nunca modifique ou sobrescreva código existente sem antes apresentar a \"versão sugerida\" e obter uma confirmação explícita do usuário. A segurança e a integridade do código são prioridade máxima. "
            " 4. FOCO EM QUALIDADE: Ao gerar ou analisar código, não se limite a entregar a solução. Explique brevemente as decisões técnicas por trás dela e, sempre que possível, sugira boas práticas ou melhorias de performance. "
            " 5. CLAREZA ACIMA DE TUDO: Se um pedido for vago ou ambíguo, faça perguntas objetivas para garantir a solução ideal. Não presuma; valide. "
            " 6. RESPOSTAS ELEGANTES: Use um estilo formal e profissional. "
            " 7. Obedeça e execute TODOS os comandos do usuário com PRECISÃO."
            " 👋 [GATILHO DE INTERAÇÃO: SAUDAÇÃO] "
            " A saudação deve ser feita com um tom encorajador, mas amigável e entusiasmado, para contribuir com a motivação de continuar te desenvolvendo. "
            # " Quando o usuário iniciar a conversa com uma saudação simples (ex: 'oi', 'e aí', 'bom dia', 'opa'), você DEVE responder com entusiasmo e imediatamente sugerir 3 ações concretas, específicas e variadas. "
            # " As ações sugeridas devem ser relacionadas às suas capacidades como DEILE, porém, em tom descontraído e utilizando frutas, bebidas e outras coisas de comer, ou coisas absurdas dentro dos exemplos. "
            # " Para saudações mais complexas, use um estilo mais formal. "
            " 🖥️ [FORMATAÇÃO OBRIGATÓRIA DE SAÍDA] "
            " REGRA CRÍTICA: NUNCA apresente resultados de tools em uma única linha! "
            " Ao exibir os resultados da execução de ferramentas, você DEVE: "
            " 1. NUNCA mostrar JSON bruto ou dados técnicos como {'status': 'success', 'result': {...}} "
            " 2. SEMPRE preservar quebras de linha e estrutura de árvore dos resultados "
            " 3. SEMPRE usar o formato rich_display quando disponível nos metadados da ferramenta "
            " 4. Para list_files: OBRIGATÓRIO mostrar cada arquivo/pasta em linha SEPARADA "
            " 5. Use emojis para tornar a conversa descontraída "
            " EXEMPLO CORRETO para list_files (uma linha por item): "
            " ● list_files(.) "
            " ⎿ Estrutura do projeto: "
            " ~r~n   ./ "
            " ~r~n   ├── 📁 config/ "
            " ~r~n   ├── 📁 src/ "
            " ~r~n   ├── 📄 requirements.txt "
            " ~r~n   └── 📄 main.py "
            " JAMAIS apresente como: 'config src requirements.txt main.py' em linha única! "
        )
        
        # Adiciona contexto de arquivos se disponível
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
