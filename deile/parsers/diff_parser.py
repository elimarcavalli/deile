"""Parser para diffs e patches (implementação futura)"""

import re
from typing import List, Optional

from .base import RegexParser, ParseResult, ParseStatus, ParsedCommand
from ..core.exceptions import ParserError


class DiffParser(RegexParser):
    """Parser para diffs/patches (preparado para implementação futura)"""
    
    def __init__(self):
        # Padrões para diferentes formatos de diff
        diff_patterns = [
            r'^\+\+\+ (.+)$',  # Cabeçalho de arquivo novo
            r'^--- (.+)$',     # Cabeçalho de arquivo antigo
            r'^@@.*@@',        # Marcador de hunk
            r'^[\+\-].*',      # Linhas de diff
        ]
        
        super().__init__([re.compile(pattern, re.MULTILINE) for pattern in diff_patterns])
    
    @property
    def name(self) -> str:
        return "diff_parser"
    
    @property
    def description(self) -> str:
        return "Parses diff/patch format for selective code changes"
    
    @property
    def patterns(self) -> List[str]:
        return [
            r'^\+\+\+ (.+)$',
            r'^--- (.+)$',
            r'^@@.*@@',
            r'^[\+\-].*'
        ]
    
    @property
    def priority(self) -> int:
        return 70  # Prioridade média
    
    def can_parse(self, input_text: str) -> bool:
        """Verifica se o texto contém formato de diff"""
        # Verifica se contém marcadores típicos de diff
        diff_indicators = ['+++', '---', '@@', 'diff --git']
        return any(indicator in input_text for indicator in diff_indicators)
    
    def parse(self, input_text: str) -> ParseResult:
        """Parseia formato de diff (implementação básica)"""
        self._parse_count += 1
        
        try:
            # Por enquanto, apenas detecta que é um diff
            # Implementação completa será adicionada quando a funcionalidade for necessária
            
            if not self.can_parse(input_text):
                return self._create_error_result("Input is not a valid diff format")
            
            commands = []
            file_references = []
            
            # Extrai nomes de arquivos do diff
            for line in input_text.split('\n'):
                if line.startswith('+++') or line.startswith('---'):
                    # Extrai nome do arquivo
                    parts = line.split('\t')[0].split(' ', 1)
                    if len(parts) > 1:
                        file_path = parts[1]
                        if file_path not in ['/dev/null', 'a/', 'b/'] and file_path not in file_references:
                            file_references.append(file_path)
            
            # Cria comando para aplicar diff
            if file_references:
                parsed_command = ParsedCommand(
                    action="apply_diff",
                    target=file_references[0] if file_references else None,
                    arguments={
                        "diff_content": input_text,
                        "affected_files": file_references
                    },
                    raw_text=input_text
                )
                commands.append(parsed_command)
            
            return ParseResult(
                status=ParseStatus.SUCCESS if commands else ParseStatus.PARTIAL,
                commands=commands,
                file_references=file_references,
                tool_requests=["diff_tool"] if commands else [],
                confidence=0.8 if commands else 0.3,
                metadata={
                    "parser": self.name,
                    "diff_type": "unified_diff",
                    "files_affected": len(file_references)
                }
            )
            
        except Exception as e:
            return ParseResult(
                status=ParseStatus.FAILED,
                error_message=f"Error parsing diff: {str(e)}",
                metadata={"parser": self.name, "error": str(e)}
            )
    
    def get_confidence(self, input_text: str) -> float:
        """Calcula confiança baseada na estrutura do diff"""
        if not self.can_parse(input_text):
            return 0.0
        
        confidence = 0.5  # Base
        
        # Aumenta confiança baseado em marcadores de diff
        if '+++' in input_text and '---' in input_text:
            confidence += 0.2
        if '@@' in input_text:
            confidence += 0.2
        if input_text.count('\n+') > 0 or input_text.count('\n-') > 0:
            confidence += 0.1
        
        return min(confidence, 1.0)
    
    async def get_suggestions(self, partial_input: str) -> List[str]:
        """Não aplicável para diffs"""
        return []