"""Carregador de instruções de arquivos MD"""

import logging
from pathlib import Path
from typing import Dict, Optional
import time

logger = logging.getLogger(__name__)


class InstructionLoader:
    """Carrega instruções de sistema a partir de arquivos Markdown

    Responsável por:
    - Carregar instruções de arquivos .md
    - Cache para performance
    - Hot-reload quando arquivos mudarem
    - Fallback para instruções padrão
    """

    def __init__(self, instructions_dir: Optional[Path] = None):
        self.instructions_dir = instructions_dir or Path("deile/personas/instructions")
        self.instructions_dir.mkdir(parents=True, exist_ok=True)

        # Cache de instruções carregadas
        self._cache: Dict[str, str] = {}
        self._file_mtimes: Dict[str, float] = {}

        logger.info(f"InstructionLoader inicializado. Diretório: {self.instructions_dir}")

    def load_instruction(self, instruction_name: str) -> Optional[str]:
        """Carrega uma instrução específica de arquivo MD

        Args:
            instruction_name: Nome do arquivo (sem extensão .md)

        Returns:
            Conteúdo da instrução ou None se não encontrada
        """
        file_path = self.instructions_dir / f"{instruction_name}.md"

        try:
            # Verifica se arquivo existe
            if not file_path.exists():
                logger.warning(f"Arquivo de instrução não encontrado: {file_path}")
                return None

            # Verifica se precisa recarregar (hot-reload)
            current_mtime = file_path.stat().st_mtime
            cache_key = str(file_path)

            if (cache_key in self._cache and
                cache_key in self._file_mtimes and
                self._file_mtimes[cache_key] == current_mtime):
                # Retorna do cache
                logger.debug(f"Instrução '{instruction_name}' carregada do cache")
                return self._cache[cache_key]

            # Carrega do arquivo
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()

            # Atualiza cache
            self._cache[cache_key] = content
            self._file_mtimes[cache_key] = current_mtime

            logger.info(f"Instrução '{instruction_name}' carregada de {file_path}")
            return content

        except Exception as e:
            logger.error(f"Erro ao carregar instrução '{instruction_name}': {e}")
            return None

    def load_fallback_instruction(self) -> str:
        """Carrega instrução de fallback padrão

        Returns:
            Instrução de fallback ou mensagem de erro mínima
        """
        # Tenta carregar fallback.md
        fallback_content = self.load_instruction("fallback")
        if fallback_content:
            return fallback_content

        # Tenta carregar default.md
        default_content = self.load_instruction("default")
        if default_content:
            return default_content

        # Fallback final mínimo (apenas se arquivos MD não existem)
        logger.error("Nenhum arquivo de instrução encontrado! Usando fallback de emergência.")
        return self._get_emergency_fallback()

    def _get_emergency_fallback(self) -> str:
        """Fallback de emergência quando nenhum arquivo MD existe"""
        return (
            "Você é DEILE, um assistente de IA especializado em desenvolvimento de software. "
            "Execute tarefas de programação de forma autônoma e eficiente. "
            "Use suas ferramentas automaticamente quando necessário."
        )

    def get_available_instructions(self) -> list[str]:
        """Lista todas as instruções disponíveis

        Returns:
            Lista de nomes de instruções (sem extensão .md)
        """
        md_files = list(self.instructions_dir.glob("*.md"))
        return [f.stem for f in md_files]

    def clear_cache(self) -> None:
        """Limpa o cache de instruções"""
        self._cache.clear()
        self._file_mtimes.clear()
        logger.debug("Cache de instruções limpo")

    def get_stats(self) -> Dict[str, any]:
        """Retorna estatísticas do loader"""
        return {
            "instructions_dir": str(self.instructions_dir),
            "cached_instructions": len(self._cache),
            "available_instructions": len(self.get_available_instructions()),
            "instruction_files": self.get_available_instructions()
        }